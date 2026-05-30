from flask import Flask, request, abort

from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TemplateMessage,
    ButtonsTemplate,
    CarouselTemplate,
    CarouselColumn,
    PostbackAction,
    URIAction,
    TextMessage,
    QuickReply,
    QuickReplyItem
)
from linebot.v3.webhooks import (
    MessageEvent,
    FollowEvent,
    PostbackEvent,
    TextMessageContent
)
import urllib.parse
import psycopg2
import os
import re

app = Flask(__name__)
DB_URL = os.environ.get('POSTGRES_URL') 

configuration = Configuration(access_token=os.getenv('CHANNEL_ACCESS_TOKEN'))
line_handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))

# ==================== Neon PostgreSQL 資料庫操作邏輯 ====================

def get_db_connection():
    if not DB_URL:
        raise ValueError("環境變數 POSTGRES_URL 未設定！")
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # 1. 口袋名單表 (新增 map_url 欄位)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pocket_list (
            user_id TEXT,
            restaurant_name TEXT,
            PRIMARY KEY (user_id, restaurant_name)
        )
    ''')
    # 💡 核心安全升級：檢查並動態為舊資料表加上 map_url 欄位，預設為空 (NULL)
    try:
        cursor.execute('ALTER TABLE pocket_list ADD COLUMN map_url TEXT;')
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback() # 如果欄位已經存在，會噴錯，我們直接 rollback 跳過即可

    # 2. 使用者狀態表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            state TEXT
        )
    ''')
    # 3. 標籤資料表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS restaurant_tags (
            user_id TEXT,
            restaurant_name TEXT,
            tag TEXT,
            PRIMARY KEY (user_id, restaurant_name, tag)
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()

def is_restaurant_exist(user_id, restaurant_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT 1 FROM pocket_list WHERE user_id = %s AND restaurant_name = %s',
        (user_id, restaurant_name)
    )
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return result is not None

def add_restaurant(user_id, restaurant_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO pocket_list (user_id, restaurant_name) 
            VALUES (%s, %s)
            ON CONFLICT (user_id, restaurant_name) DO NOTHING
        ''', (user_id, restaurant_name))
        success = cursor.rowcount > 0
        conn.commit()
    except Exception as e:
        print(f"Database Error: {e}")
        success = False
    finally:
        cursor.close()
        conn.close()
    return success

# 新增：更新餐廳的 Google Maps 地圖連結
def update_restaurant_url(user_id, restaurant_name, map_url):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE pocket_list 
            SET map_url = %s 
            WHERE user_id = %s AND restaurant_name = %s
        ''', (map_url, user_id, restaurant_name))
        conn.commit()
        return True
    except Exception as e:
        print(f"URL Update Error: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

def delete_restaurant(user_id, restaurant_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM restaurant_tags WHERE user_id = %s AND restaurant_name = %s', (user_id, restaurant_name))
    cursor.execute('DELETE FROM pocket_list WHERE user_id = %s AND restaurant_name = %s', (user_id, restaurant_name))
    changes = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    return changes > 0

# 修改：除了撈出名字，也要一併撈出 map_url
def get_user_pocket_list(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT restaurant_name, map_url FROM pocket_list WHERE user_id = %s', (user_id,))
        rows = cursor.fetchall()
        # 回傳格式為陣列包字典： [{'name': '...', 'url': '...'}, ...]
        return [{'name': row[0], 'url': row[1]} for row in rows]
    except Exception:
        return []
    finally:
        cursor.close()
        conn.close()

# 新增：從資料庫隨機抽取一間餐廳 (ORDER BY RANDOM)
def get_random_restaurant(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT restaurant_name, map_url 
            FROM pocket_list 
            WHERE user_id = %s 
            ORDER BY RANDOM() 
            LIMIT 1
        ''', (user_id,))
        row = cursor.fetchone()
        return {'name': row[0], 'url': row[1]} if row else None
    except Exception:
        return None
    finally:
        cursor.close()
        conn.close()

def get_restaurant_tags(user_id, restaurant_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT tag FROM restaurant_tags WHERE user_id = %s AND restaurant_name = %s',
            (user_id, restaurant_name)
        )
        rows = cursor.fetchall()
        return [row[0] for row in rows]
    except Exception:
        return []
    finally:
        cursor.close()
        conn.close()

def add_restaurant_tags(user_id, restaurant_name, tags_list):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for tag in tags_list:
            if tag.strip():
                cursor.execute('''
                    INSERT INTO restaurant_tags (user_id, restaurant_name, tag)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, restaurant_name, tag) DO NOTHING
                ''', (user_id, restaurant_name, tag.strip()))
        conn.commit()
        return True
    except Exception as e:
        print(f"Tag Database Error: {e}")
        return False
    finally:
        cursor.close()
        conn.close()

def set_user_state(user_id, state):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_state (user_id, state) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state
    ''', (user_id, state))
    conn.commit()
    cursor.close()
    conn.close()

def get_user_state(user_id):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT state FROM user_state WHERE user_id = %s', (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else 'IDLE'

# ====================================================================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@line_handler.add(FollowEvent)
def handle_follow(event):
    send_reply(event.reply_token, [TextMessage(text="嗨！我是你的口袋名單助手，請選擇你想執行的功能：")], menu_type='main')


def get_main_quick_reply():
    """修改：主選單新增「🎲 隨機推薦」功能"""
    return QuickReply(
        items=[
            QuickReplyItem(action=PostbackAction(label="➕ 加入餐廳", data="menu_action=click_add", displayText="點擊了加入餐廳")),
            QuickReplyItem(action=PostbackAction(label="📋 我的口袋名單", data="menu_action=click_list&page=1", displayText="查看口袋名單")),
            QuickReplyItem(action=PostbackAction(label="🎲 隨機推薦", data="menu_action=click_random", displayText="幫我隨機推薦一間餐廳"))
        ]
    )

def get_list_quick_reply(total_count, page=1):
    page_size = 10
    has_prev = page > 1
    has_next = (page * page_size) < total_count

    items = []
    if has_prev:
        items.append(QuickReplyItem(action=PostbackAction(label="⬅️ 上一頁", data=f"menu_action=click_list&page={page - 1}", displayText="查看上一頁名單")))
    if has_next:
        items.append(QuickReplyItem(action=PostbackAction(label="➡️ 下一頁", data=f"menu_action=click_list&page={page + 1}", displayText="查看下一頁名單")))
    
    items.append(QuickReplyItem(action=PostbackAction(label="🚪 退出名單", data="menu_action=exit_list", displayText="退出名單模式")))
    return QuickReply(items=items)


# ==================== 輪播圖卡產生邏輯（修正安全編碼版） ====================
def get_carousel_list_message(user_id, user_list, page=1):
    total_count = len(user_list)
    page_size = 10
    
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    current_page_items = user_list[start_idx:end_idx]

    columns = []
    for idx, res_data in enumerate(current_page_items, start=start_idx + 1):
        name = res_data['name']
        url = res_data['url']  # 資料庫撈出來的網址
        
        tags = get_restaurant_tags(user_id, name)
        tag_text = " ".join([f"#{t}" for t in tags]) if tags else "暫無標籤"
        display_title = name[:40]

        # 固定必有的兩個按鈕
        card_actions = [
            PostbackAction(label="🏷️ 加入標籤", data=f"action=click_add_tag&name={urllib.parse.quote(name)}", displayText=f"想為 {name} 新增標籤"),
            PostbackAction(label="❌ 刪除這間餐廳", data=f"action=ask_delete&name={urllib.parse.quote(name)}", displayText=f"想要移除 {name}")
        ]

        # 根據 url 是否存在，動態塞入第 3 個按鈕
        if url:
            try:
                # 💡 核心修正：如果原本存的是舊格式，我們重新將它用官方標準安全格式包裝
                # 提取出網址後方的座標或名稱
                if "maps.google.com/7" in url:
                    raw_query = url.split("maps.google.com/7")[-1]
                else:
                    raw_query = url
                
                # 使用 Google 官方標準 Search API 格式，並進行安全網址編碼，保證 LINE 絕不卡死
                safe_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(raw_query)}"
                card_actions.insert(0, URIAction(label="🌐 開啟地圖", uri=safe_url))
            except Exception as e:
                print(f"URL Encode Error: {e}")
                card_actions.insert(0, PostbackAction(label="📍 加入地點", data=f"action=click_add_url&name={urllib.parse.quote(name)}", displayText=f"為 {name} 設定地點"))
        else:
            # 沒網址，引導去輸入地點座標
            card_actions.insert(0, PostbackAction(label="📍 加入地點", data=f"action=click_add_url&name={urllib.parse.quote(name)}", displayText=f"為 {name} 設定地點"))

        columns.append(
            CarouselColumn(
                title=display_title,
                text=f"{tag_text}\n({idx}/{total_count})",
                actions=card_actions
            )
        )

    carousel_template = CarouselTemplate(columns=columns)
    return TemplateMessage(alt_text="你的口袋名單輪播", template=carousel_template)


@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id 
    current_state = get_user_state(user_id)

    # ------ 狀態 A：等待使用者輸入「要加入的餐廳名稱」 ------
    if current_state == 'WAIT_FOR_ADD':
        restaurant_name = user_message
        set_user_state(user_id, 'IDLE')
        
        buttons_template = ButtonsTemplate(
            title="確認加入名單",
            text=f"確定要將「{restaurant_name}」加入口袋名單嗎?",
            actions=[
                PostbackAction(label="確認加入", data=f"action=add&name={urllib.parse.quote(restaurant_name)}", displayText=f"確認加入 {restaurant_name}"),
                PostbackAction(label="取消", data="action=cancel", displayText="取消操作")
            ]
        )
        send_reply(event.reply_token, [TemplateMessage(alt_text="請確認是否加入口袋名單", template=buttons_template)])
        return

    # ------ 狀態 C：等待使用者輸入「標籤」 ------
    elif current_state.startswith('WAIT_FOR_TAG|'):
        _, target_restaurant = current_state.split('|', 1)
        set_user_state(user_id, 'IDLE')

        raw_input = user_message.replace('#', ' ')
        input_tags = [t.strip() for t in raw_input.split() if t.strip()]

        if not input_tags:
            send_reply(event.reply_token, [TextMessage(text="未偵測到有效的標籤，操作已取消。")], menu_type='main')
            return

        tags_str = " ".join([f"#{t}" for t in input_tags])
        encoded_tags = urllib.parse.quote(",".join(input_tags))

        buttons_template = ButtonsTemplate(
            title="確認新增標籤",
            text=f"要為「{target_restaurant}」加上標籤嗎？\n{tags_str}",
            actions=[
                PostbackAction(label="確認加入標籤", data=f"action=tag_confirm&name={urllib.parse.quote(target_restaurant)}&tags={encoded_tags}", displayText="確認新增標籤"),
                PostbackAction(label="取消", data="action=cancel", displayText="取消操作")
            ]
        )
        send_reply(event.reply_token, [TemplateMessage(alt_text="請確認是否加入標籤", template=buttons_template)])
        return

    # ------ 修改狀態 D：等待使用者輸入「經緯度座標」 ------
    elif current_state.startswith('WAIT_FOR_URL|'):
        _, target_restaurant = current_state.split('|', 1)
        set_user_state(user_id, 'IDLE')

        # 💡 正規表達式：允許 [正負號][數字][小數點] + [逗號] + [正負號][數字][小數點]
        # 這樣可以完美匹配像 "25.0339, 121.5645" 或 "25.0339,121.5645" 這種乾淨的字串
        coord_pattern = r'^[-+]?([1-8]?\d(\.\d+)?|90(\.0+)?),\s*[-+]?(180(\.0+)?|((1[0-7]\d)|([1-9]?\d))(\.\d+)?)$'
        
        # 先把全形逗號換成半形逗號，拿掉前後空格
        cleaned_message = user_message.replace('，', ',').strip()

        if not re.match(coord_pattern, cleaned_message):
            error_text = (
                "⚠️ 格式錯誤，操作已取消！\n\n"
                "請確保輸入的是乾淨的經緯度座標數字。\n"
                "正確格式範例：\n"
                "25.0339, 121.5645"
            )
            send_reply(event.reply_token, [TextMessage(text=error_text)], menu_type='main')
            return

        # 格式完全正確，直接將拼好的 Google Maps 導航連結準備好
        # 用經緯度導航的官方萬用格式：https://www.google.com/maps/search/?api=1&query=緯度,經度
        target_url = cleaned_message

        # 彈出確認視窗
        buttons_template = ButtonsTemplate(
            title="確認位置設定",
            text=f"已成功識別座標！要為「{target_restaurant}」綁定這個地圖位置嗎？",
            actions=[
                PostbackAction(
                    label="確認設定位置", 
                    data=f"action=url_confirm&name={urllib.parse.quote(target_restaurant)}&url={urllib.parse.quote(target_url)}", 
                    displayText="確認設定位置"
                ),
                PostbackAction(label="取消", data="action=cancel", displayText="取消操作")
            ]
        )
        send_reply(event.reply_token, [TemplateMessage(alt_text="請確認是否綁定地圖位置", template=buttons_template)])
        return

    # ------ 一般狀態 (IDLE) 底下的關鍵字相容 ------
    if user_message == '我的口袋名單':
        user_list = get_user_pocket_list(user_id)
        if not user_list:
            send_reply(event.reply_token, [TextMessage(text="目前的口袋名單空空如也喔！快去加入餐廳吧。")], menu_type='main')
        else:
            carousel_msg = get_carousel_list_message(user_id, user_list, page=1)
            hint_msg = TextMessage(text="已進入名單模式，您可以使用下方選單切換頁面或退出：")
            send_reply(event.reply_token, [carousel_msg, hint_msg], menu_type='list', total_count=len(user_list), page=1)
    else:
        send_reply(event.reply_token, [TextMessage(text="請點選下方選單來操作喔！")], menu_type='main')


@line_handler.add(PostbackEvent)
def handle_postback(event):
    postback_data = event.postback.data
    user_id = event.source.user_id
    
    params = dict(urllib.parse.parse_qsl(postback_data))
    menu_action = params.get('menu_action')
    action = params.get('action')
    
    # ================= 1. 處理 Quick Reply 與選單動作 =================
    if menu_action == 'click_add':
        set_user_state(user_id, 'WAIT_FOR_ADD')
        send_reply(event.reply_token, [TextMessage(text="請直接輸入你想加入的餐廳名稱：")])
        return
        
    elif menu_action == 'click_list':
        page = int(params.get('page', 1))
        user_list = get_user_pocket_list(user_id)
        if not user_list:
            send_reply(event.reply_token, [TextMessage(text="目前的口袋名單空空如也喔！快去加入餐廳吧。")], menu_type='main')
        else:
            carousel_msg = get_carousel_list_message(user_id, user_list, page=page)
            hint_msg = TextMessage(text=f"目前在第 {page} 頁，可繼續點選下方選單：")
            send_reply(event.reply_token, [carousel_msg, hint_msg], menu_type='list', total_count=len(user_list), page=page)
        return

    elif menu_action == 'exit_list':
        send_reply(event.reply_token, [TextMessage(text="已退出名單模式，回到主選單。")], menu_type='main')
        return

    # 修改後：處理主選單點擊「🎲 隨機推薦」
    elif menu_action == 'click_random':
        res_data = get_random_restaurant(user_id)
        
        if not res_data:
            send_reply(event.reply_token, [TextMessage(text="你的口袋名單目前沒有任何餐廳，抽不到東西喔！")], menu_type='main')
        else:
            restaurant_name = res_data['name']  
            db_url = res_data['url'] # 從資料庫撈出來的網址(可能為 None)
            
            # 💡 核心智慧判斷：有綁定座標網址就用它，沒有的話就動態退化成名稱自動搜尋
            if db_url:
                maps_url = db_url
                nav_label = "🗺️ 開始導航 (精準座標)"
            else:
                encoded_name = urllib.parse.quote(restaurant_name)
                maps_url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}"
                nav_label = "🗺️ 開始導航 (名稱搜尋)"
            
            column = CarouselColumn(
                title=f"🎲 今日推薦：{restaurant_name[:30]}",
                text="為您隨機挑選的美味，點擊下方開始導航吧！",
                actions=[
                    URIAction(label=nav_label, uri=maps_url),
                    PostbackAction(
                        label="❌ 刪除餐廳",
                        data=f"action=delete_confirm&name={urllib.parse.quote(restaurant_name)}",
                        displayText=f"確認刪除 {restaurant_name}"
                    )
                ]
            )
            
            carousel_template = CarouselTemplate(columns=[column])
            template_message = TemplateMessage(
                alt_text=f"今日推薦餐廳：{restaurant_name}",
                template=carousel_template
            )
            
            send_reply(event.reply_token, [template_message, TextMessage(text="今天就決定吃這家了嗎？😋")], menu_type='main')
        return

    # ================= 2. 處理 Template 的動作 =================
    if action == 'add':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        if is_restaurant_exist(user_id, restaurant_name):
            reply_text = f"這張卡片失效囉！「{restaurant_name}」已經在名單內了。"
        else:
            add_restaurant(user_id, restaurant_name)
            reply_text = f"已成功將「{restaurant_name}」寫入口袋名單！"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], menu_type='main')
        
    elif action == 'cancel':
        send_reply(event.reply_token, [TextMessage(text="好的，已取消操作。")], menu_type='main')

    elif action == 'click_add_tag':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        set_user_state(user_id, f"WAIT_FOR_TAG|{restaurant_name}")
        send_reply(event.reply_token, [TextMessage(text=f"請輸入要為「{restaurant_name}」新增的標籤：\n(多個標籤請用空格分隔，例：#好吃 #拉麵)")])

    elif action == 'tag_confirm':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        tags_raw = urllib.parse.unquote(params.get('tags', ''))
        tags_list = tags_raw.split(',')
        
        success = add_restaurant_tags(user_id, restaurant_name, tags_list)
        if success:
            reply_text = f"成功為「{restaurant_name}」加上標籤！"
        else:
            reply_text = "新增標籤時發生系統錯誤。"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], menu_type='main')

    # 修改：點擊圖卡上的「📍 加入地點」
    elif action == 'click_add_url':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        set_user_state(user_id, f"WAIT_FOR_URL|{restaurant_name}")
        
        guide_text = (
            f"📌 請輸入「{restaurant_name}」的經緯度座標：\n\n"
            f"💡 【手機查詢小技巧】\n"
            f"1️⃣ 打開 Google 地圖，在該餐廳的位置「長按」放下一支紅針。\n"
            f"2️⃣ 螢幕下方彈出的面板就會出現一串數字（例：25.0339, 121.5645）。\n"
            f"3️⃣ 直接點擊或長按那串數字進行複製，並「單獨貼回來」這裡就可以囉！"
        )
        send_reply(event.reply_token, [TextMessage(text=guide_text)])
        return

    # 新增：處理確認寫入 URL 的資料庫更新動作
    elif action == 'url_confirm':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        target_url = urllib.parse.unquote(params.get('url', ''))
        
        success = update_restaurant_url(user_id, restaurant_name, target_url)
        if success:
            reply_text = f"成功為「{restaurant_name}」綁定地圖連結！✨\n之後查看名單就可以一鍵導航囉。"
        else:
            reply_text = "綁定連結時發生系統錯誤。"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], menu_type='main')

    elif action == 'ask_delete':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        buttons_template = ButtonsTemplate(
            title="確認刪除",
            text=f"你確定要將「{restaurant_name}」從口袋名單中移除嗎？",
            actions=[
                PostbackAction(label="確認刪除", data=f"action=delete_confirm&name={urllib.parse.quote(restaurant_name)}", displayText=f"確認刪除 {restaurant_name}"),
                PostbackAction(label="取消", data="action=delete_cancel", displayText="取消刪除")
            ]
        )
        send_reply(event.reply_token, [TemplateMessage(alt_text="請確認是否刪除餐廳", template=buttons_template)])

    elif action == 'delete_confirm':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        is_deleted = delete_restaurant(user_id, restaurant_name)
        if is_deleted:
            reply_text = f"已將「{restaurant_name}」從你的口袋名單中移除！"
        else:
            reply_text = f"這張卡片失效囉！名單內已無「{restaurant_name}」。"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], menu_type='main')
        
    elif action == 'delete_cancel':
        send_reply(event.reply_token, [TextMessage(text="好的，已取消刪除，保留餐廳。")], menu_type='main')


def send_reply(reply_token, messages, menu_type=None, total_count=0, page=1):
    if messages and isinstance(messages[-1], TextMessage):
        if menu_type == 'main':
            messages[-1].quick_reply = get_main_quick_reply()
        elif menu_type == 'list':
            messages[-1].quick_reply = get_list_quick_reply(total_count, page)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=messages)
        )

init_db()