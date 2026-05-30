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
    # 1. 口袋名單表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pocket_list (
            user_id TEXT,
            restaurant_name TEXT,
            map_url TEXT,
            PRIMARY KEY (user_id, restaurant_name)
        )
    ''')
    try:
        cursor.execute('ALTER TABLE pocket_list ADD COLUMN map_url TEXT;')
        conn.commit()
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    # 2. 使用者狀態表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            state TEXT
        )
    ''')
    
    # 3. 標籤資料表 (#開頭)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS restaurant_tags (
            user_id TEXT,
            restaurant_name TEXT,
            tag TEXT,
            PRIMARY KEY (user_id, restaurant_name, tag)
        )
    ''')

    # 💡 新增 4. 分類資料表 (/開頭)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS restaurant_categories (
            user_id TEXT,
            restaurant_name TEXT,
            category TEXT,
            PRIMARY KEY (user_id, restaurant_name, category)
        )
    ''')

    # 💡 新增 5. 用餐冷卻歷史紀錄表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_cooling_history (
            id SERIAL PRIMARY KEY,
            user_id TEXT,
            restaurant_name TEXT,
            visited_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 💡 新增 6. 使用者計數統計表（專門記錄無法從現有資料撈出的數據，如：刪除次數）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id TEXT PRIMARY KEY,
            deleted_count INTEGER DEFAULT 0
        )
    ''')

    conn.commit()
    cursor.close()
    conn.close()

# 新增：寫入用餐歷史紀錄
def add_to_cooling_history(user_id, restaurant_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'INSERT INTO user_cooling_history (user_id, restaurant_name) VALUES (%s, %s)',
            (user_id, restaurant_name)
        )
        conn.commit()
    except Exception as e:
        print(f"Cooling history error: {e}")
    finally:
        cursor.close()
        conn.close()

# 新增：動態取得使用者建立過的所有分類清單 (不重複)
def get_user_all_categories(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT DISTINCT category FROM restaurant_categories WHERE user_id = %s', (user_id,))
        rows = cursor.fetchall()
        return [row[0] for row in rows]
    except Exception:
        return []
    finally:
        cursor.close()
        conn.close()

# 新增：寫入分類資料
def add_restaurant_category(user_id, restaurant_name, category_name):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO restaurant_categories (user_id, restaurant_name, category)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, restaurant_name, category) DO NOTHING
        ''', (user_id, restaurant_name, category_name))
        conn.commit()
    except Exception as e:
        print(f"Category DB error: {e}")
    finally:
        cursor.close()
        conn.close()

# 新增：根據特定分類篩選口袋名單
def get_pocket_list_by_category(user_id, category=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if not category or category == '全部':
            cursor.execute('SELECT restaurant_name, map_url FROM pocket_list WHERE user_id = %s', (user_id,))
        else:
            cursor.execute('''
                SELECT p.restaurant_name, p.map_url 
                FROM pocket_list p
                JOIN restaurant_categories c ON p.user_id = c.user_id AND p.restaurant_name = c.restaurant_name
                WHERE p.user_id = %s AND c.category = %s
            ''', (user_id, category))
        rows = cursor.fetchall()
        return [{'name': row[0], 'url': row[1]} for row in rows]
    except Exception:
        return []
    finally:
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

    if changes > 0:
        cursor.execute('''
            INSERT INTO user_stats (user_id, deleted_count) VALUES (%s, 1)
            ON CONFLICT (user_id) DO UPDATE SET deleted_count = user_stats.deleted_count + 1
        ''', (user_id,))

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
# 修改：隨機推薦抽出時，自動排除最近 5 次吃過的餐廳
def get_random_restaurant(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            SELECT restaurant_name, map_url 
            FROM pocket_list 
            WHERE user_id = %s 
              AND restaurant_name NOT IN (
                  SELECT restaurant_name 
                  FROM user_cooling_history 
                  WHERE user_id = %s 
                  ORDER BY visited_at DESC 
                  LIMIT 5
              )
            ORDER BY RANDOM() 
            LIMIT 1
        ''', (user_id, user_id))
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

# 新增：動態統計使用者的各項操作數據
def get_user_achievement_stats(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    stats = {'added': 0, 'visited': 0, 'deleted': 0}
    try:
        # 1. 統計目前名單內的餐廳數 (新增次數)
        cursor.execute('SELECT COUNT(*) FROM pocket_list WHERE user_id = %s', (user_id,))
        stats['added'] = cursor.fetchone()[0]
        
        # 2. 統計總用餐次數
        cursor.execute('SELECT COUNT(*) FROM user_cooling_history WHERE user_id = %s', (user_id,))
        stats['visited'] = cursor.fetchone()[0]
        
        # 3. 讀取總刪除次數
        cursor.execute('SELECT deleted_count FROM user_stats WHERE user_id = %s', (user_id,))
        row = cursor.fetchone()
        stats['deleted'] = row[0] if row else 0
        
    except Exception as e:
        print(f"Achievement stats error: {e}")
    finally:
        cursor.close()
        conn.close()
    return stats

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
            QuickReplyItem(action=PostbackAction(label="🎲 隨機推薦", data="menu_action=click_random", displayText="幫我隨機推薦一間餐廳")),
            QuickReplyItem(action=PostbackAction(label="🏆 我的成就", data="menu_action=click_achievement", displayText="查看我的美食成就"))
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
            PostbackAction(label="🏷️ 加入#標籤 /分類", data=f"action=click_add_tag&name={urllib.parse.quote(name)}", displayText=f"想為 {name} 新增標籤"),
            PostbackAction(label="❌ 刪除這間餐廳", data=f"action=ask_delete&name={urllib.parse.quote(name)}", displayText=f"想要移除 {name}")
        ]

        # 根據 url 是否存在，動態塞入第 3 個按鈕
        if url:
            try:
                # 先把可能不小心存進資料庫的舊網址開頭清乾淨，只留下最核心的座標或名稱字串
                clean_query = url.replace("https://www.google.com/maps/search/?api=1&query=", "").strip()
                
                # 進行安全的網址編碼
                encoded_query = urllib.parse.quote(clean_query)
                
                # 🎯 使用 Google Maps 官方最標準、相容性最高的 Universal URL 格式！
                # 這個格式不論填「名稱」或「經緯度」，Google Maps 都會 100% 完美辨識導航
                safe_url = f"https://www.google.com/maps/search/?api=1&query={encoded_query}"
                
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

        # 將輸入依照空白切開
        tokens = user_message.split()
        tags_to_add = []
        categories_to_add = []

        for token in tokens:
            if token.startswith('#'):
                t_name = token[1:].strip()
                if t_name: tags_to_add.append(t_name)
            elif token.startswith('/'):
                c_name = token[1:].strip()
                if c_name: categories_to_add.append(c_name)

        if not tags_to_add and not categories_to_add:
            send_reply(event.reply_token, [TextMessage(text="未偵測到以 # 開頭的標籤或 / 開頭的分類，操作已取消。")], menu_type='main')
            return

        # 寫入資料庫
        if tags_to_add:
            add_restaurant_tags(user_id, target_restaurant, tags_to_add)
        if categories_to_add:
            for cat in categories_to_add:
                add_restaurant_category(user_id, target_restaurant, cat)

        summary = "✨ 設定成功！\n"
        if tags_to_add: summary += f"🏷️ 標籤：{' '.join(['#'+t for t in tags_to_add])}\n"
        if categories_to_add: summary += f"📂 分類：{' '.join(['/'+c for c in categories_to_add])}"

        send_reply(event.reply_token, [TextMessage(text=summary)], menu_type='main')
        return

    # ------ 修改狀態 D：等待使用者輸入「經緯度座標」 ------
    elif current_state.startswith('WAIT_FOR_URL|'):
        _, target_restaurant = current_state.split('|', 1)
        set_user_state(user_id, 'IDLE')

        # 💡 先把可能包含的 ( ) 括號拿掉，並將全形逗號換成半形
        cleaned_message = user_message.replace('(', '').replace(')', '').replace('（', '').replace('）', '').replace('，', ',').strip()

        # 正規表達式驗證乾淨的經緯度
        coord_pattern = r'^[-+]?([1-8]?\d(\.\d+)?|90(\.0+)?),\s*[-+]?(180(\.0+)?|((1[0-7]\d)|([1-9]?\d))(\.\d+)?)$'
        
        if not re.match(coord_pattern, cleaned_message):
            error_text = "⚠️ 格式錯誤！請確保輸入的是正確的經緯度座標數字（帶有括號也可以哦）。\n範例：(25.0339, 121.5645)"
            send_reply(event.reply_token, [TextMessage(text=error_text)], menu_type='main')
            return

        target_url = cleaned_message 

        buttons_template = ButtonsTemplate(
            title="確認位置設定",
            text=f"已成功識別座標！要為「{target_restaurant}」綁定這個地圖位置嗎？",
            actions=[
                PostbackAction(label="確認設定位置", data=f"action=url_confirm&name={urllib.parse.quote(target_restaurant)}&url={urllib.parse.quote(target_url)}", displayText="確認設定位置"),
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
        # 💡 核心優化：不再直接吐出名單，而是先讓使用者選分類
        user_cats = get_user_all_categories(user_id)
        
        # 組合 Quick Reply 按鈕
        items = [QuickReplyItem(action=PostbackAction(label="🌟 全部餐廳", data="menu_action=show_cat_list&cat=全部&page=1", displayText="查看全部口袋名單"))]
        for cat in user_cats[:12]: # LINE 限制 Quick Reply 最多 13 個按鈕
            items.append(QuickReplyItem(action=PostbackAction(label=f"📂 {cat}", data=f"menu_action=show_cat_list&cat={urllib.parse.quote(cat)}&page=1", displayText=f"查看分類【{cat}】")))
        
        items.append(QuickReplyItem(action=PostbackAction(label="🚪 返回主選單", data="menu_action=exit_list", displayText="返回主選單")))
        
        quick_reply_menu = QuickReply(items=items)
        msg = TextMessage(text="請選擇你想查看的餐廳分類：", quick_reply=quick_reply_menu)
        
        # 直接使用原生 Reply 傳送
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[msg]))
        return

    elif menu_action == 'show_cat_list':
        target_cat = urllib.parse.unquote(params.get('cat', '全部'))
        page = int(params.get('page', 1))
        
        user_list = get_pocket_list_by_category(user_id, target_cat)
        if not user_list:
            send_reply(event.reply_token, [TextMessage(text=f"分類【{target_cat}】目前沒有餐廳喔！")], menu_type='main')
        else:
            carousel_msg = get_carousel_list_message(user_id, user_list, page=page)
            # 為了讓分頁能記住目前在選哪個分類，稍微調整 QuickReply 的傳值
            hint_msg = TextMessage(text=f"📂 當前分類：{target_cat} (第 {page} 頁)")
            
            # 動態覆寫下一頁的 QuickReply
            page_size = 10
            has_prev = page > 1
            has_next = (page * page_size) < len(user_list)
            qr_items = []
            if has_prev: qr_items.append(QuickReplyItem(action=PostbackAction(label="⬅️ 上一頁", data=f"menu_action=show_cat_list&cat={urllib.parse.quote(target_cat)}&page={page - 1}")))
            if has_next: qr_items.append(QuickReplyItem(action=PostbackAction(label="➡️ 下一頁", data=f"menu_action=show_cat_list&cat={urllib.parse.quote(target_cat)}&page={page + 1}")))
            qr_items.append(QuickReplyItem(action=PostbackAction(label="🚪 換分類", data="menu_action=click_list")))
            hint_msg.quick_reply = QuickReply(items=qr_items)
            
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[carousel_msg, hint_msg]))
        return

    elif menu_action == 'exit_list':
        send_reply(event.reply_token, [TextMessage(text="已退出名單模式，回到主選單。")], menu_type='main')
        return

    # 修改後：處理主選單點擊「🎲 隨機推薦」
    elif menu_action == 'click_random':
        res_data = get_random_restaurant(user_id)
        
        if not res_data:
            send_reply(event.reply_token, [TextMessage(text="名單內沒有餐廳，或符合冷卻條件的餐廳不夠抽喔！")], menu_type='main')
        else:
            restaurant_name = res_data['name']  
            db_url = res_data['url']
            
            if db_url:
                clean_coord = db_url.replace("https://www.google.com/maps/search/?api=1&query=", "").strip()
                maps_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(clean_coord)}"
                nav_label = "🗺️ 開始導航 (精準座標)"
            else:
                encoded_name = urllib.parse.quote(restaurant_name)
                maps_url = f"https://www.google.com/maps/search/?api=1&query={encoded_name}"
                nav_label = "🗺️ 開始導航 (名稱搜尋)"
            
            column = CarouselColumn(
                title=f"🎲 今日推薦：{restaurant_name[:30]}",
                text="為您隨機挑選的美味，點擊下方出發吧！",
                actions=[
                    URIAction(label=nav_label, uri=maps_url),
                    # 💡 核心變更：移除刪除，改為前往用餐
                    PostbackAction(
                        label="🍽️ 前往用餐 (有冷卻)",
                        data=f"action=go_eat&name={urllib.parse.quote(restaurant_name)}",
                        displayText=f"決定去吃 {restaurant_name} 囉！"
                    )
                ]
            )
            
            carousel_template = CarouselTemplate(columns=[column])
            template_message = TemplateMessage(alt_text=f"今日推薦餐廳：{restaurant_name}", template=carousel_template)
            send_reply(event.reply_token, [template_message, TextMessage(text="今天就決定吃這家了嗎？😋")], menu_type='main')
        return
    
    # 新增：處理點擊「🏆 我的成就」
    elif menu_action == 'click_achievement':
        # 1. 撈取目前統計數據
        stats = get_user_achievement_stats(user_id)
        added = stats['added']
        visited = stats['visited']
        deleted = stats['deleted']
        
        # 2. 輔助函式：用來產生精美的勳章與進度文字
        def make_row(current, milestones, icons):
            # milestones 傳入 [1, 3, 5] 或 [1, 5, 10]
            # icons 傳入對應的表情符號
            res = ""
            for i, goal in enumerate(milestones):
                if current >= goal:
                    res += f"{icons[i]} "  # 已達成顯示勳章
                else:
                    res += "🔒 "  # 未達成顯示鎖頭
            return res

        # 3. 開始拼裝文字介面
        ach_text = (
            "🏆 【美食大師 - 成就進度面板】\n"
            "---------------------------\n\n"
            
            f"🍽️ 【出發用餐】\n"
            f"進度：{visited} 次\n"
            f"獎勵：{make_row(visited, [1, 3, 5], ['🥉', '🥈', '🥇'])}\n"
            f"🎯 目標：1次(🥉) / 3次(🥈) / 5次(🥇)\n\n"
            
            f"➕ 【開拓疆土】(新增餐廳)\n"
            f"進度：{added} 間\n"
            f"獎勵：{make_row(added, [1, 5, 10], ['🎖️', '🏅', '🏆'])}\n"
            f"🎯 目標：1間(🎖️) / 5間(🏅) / 10間(🏆)\n\n"
            
            f"❌ 【斷捨離】(刪除餐廳)\n"
            f"進度：{deleted} 次\n"
            f"獎勵：{make_row(deleted, [1, 5, 10], ['🪓', '⚔️', '👑'])}\n"
            f"🎯 目標：1次(🪓) / 5次(⚔️) / 10次(👑)\n\n"
            
            "---------------------------\n"
            "繼續使用功能，解鎖更多隱藏勳章吧！✨"
        )
        
        send_reply(event.reply_token, [TextMessage(text=ach_text)], menu_type='main')
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
        
        # 🎯 優化提示訊息：增加 /分類 的詳細說明與範例
        guide_text = (
            f"🏷️ 正在為「{restaurant_name}」設定標籤與分類\n\n"
            f"請直接輸入你想設定的內容，多個項目請用「空格」分開：\n\n"
            f"🔸 # 開頭會變成【圖卡標籤】（單純展示用）\n"
            f"🔹 / 開頭會變成【清單分類】（主選單篩選用）\n\n"
            f"💡 範例輸入（可複製修改）：\n"
            f"#美味 #有冷氣 /鍋貼 /晚餐"
        )
        send_reply(event.reply_token, [TextMessage(text=guide_text)])

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

    elif action == 'go_eat':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        # 寫入歷史紀錄，啟動 5 次排除冷卻機制
        add_to_cooling_history(user_id, restaurant_name)
        reply_text = f"👌 已幫你記錄！祝你用餐愉快！「{restaurant_name}」將進入冷卻"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], menu_type='main')
        return

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