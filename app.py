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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pocket_list (
            user_id TEXT,
            restaurant_name TEXT,
            PRIMARY KEY (user_id, restaurant_name)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            state TEXT
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

def delete_restaurant(user_id, restaurant_name):
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'DELETE FROM pocket_list WHERE user_id = %s AND restaurant_name = %s',
        (user_id, restaurant_name)
    )
    changes = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    return changes > 0

def get_user_pocket_list(user_id):
    init_db() 
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT restaurant_name FROM pocket_list WHERE user_id = %s',
            (user_id,)
        )
        rows = cursor.fetchall()
        return [row[0] for row in rows]
    except Exception:
        return []
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
    app.logger.info("Request body: " + body)
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature.")
        abort(400)
    return 'OK'

@line_handler.add(FollowEvent)
def handle_follow(event):
    send_reply(event.reply_token, [TextMessage(text="嗨！我是你的口袋名單助手，請選擇你想執行的功能：")], include_menu=True)


def get_main_quick_reply():
    """調整：移除原本的刪除按鈕，只保留加入與查看名單"""
    return QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(label="➕ 加入餐廳", data="menu_action=click_add", displayText="點擊了加入餐廳")
            ),
            QuickReplyItem(
                action=PostbackAction(label="📋 我的口袋名單", data="menu_action=click_list&page=1", displayText="查看口袋名單")
            )
        ]
    )

# ==================== 輪播圖卡產生邏輯 ====================
def get_carousel_list_message(user_list, page=1):
    """將餐廳清單轉換為 Carousel Template，支援分頁"""
    total_count = len(user_list)
    
    if total_count <= 10:
        page_size = 10
        has_next = False
    else:
        page_size = 9
        has_next = (page * page_size) < total_count

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    current_page_items = user_list[start_idx:end_idx]

    columns = []
    for idx, name in enumerate(current_page_items, start=start_idx + 1):
        columns.append(
            CarouselColumn(
                title=f"📍 餐廳名單 ({idx}/{total_count})",
                text=f"店名：{name}",
                actions=[
                    # 調整：點擊不直接刪除，而是觸發 ask_delete 進入確認畫面
                    PostbackAction(
                        label="❌ 刪除這間餐廳",
                        data=f"action=ask_delete&name={urllib.parse.quote(name)}",
                        displayText=f"想要移除 {name}"
                    )
                ]
            )
        )

    if has_next:
        columns.append(
            CarouselColumn(
                title="▶️ 還有更多餐廳喔",
                text=f"目前顯示第 {start_idx+1}~{min(end_idx, total_count)} 間",
                actions=[
                    PostbackAction(
                        label="看下一頁",
                        data=f"menu_action=click_list&page={page + 1}",
                        displayText="查看下一頁名單"
                    )
                ]
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
                PostbackAction(
                    label="確認加入",
                    data=f"action=add&name={urllib.parse.quote(restaurant_name)}",
                    displayText=f"確認加入 {restaurant_name}"
                ),
                PostbackAction(
                    label="取消",
                    data="action=cancel",
                    displayText="取消操作"
                )
            ]
        )
        send_reply(event.reply_token, [TemplateMessage(alt_text="請確認是否加入口袋名單", template=buttons_template)])
        return

    # ------ 一般狀態 (IDLE) 底下的關鍵字相容 ------
    if user_message == '我的口袋名單':
        user_list = get_user_pocket_list(user_id)
        if not user_list:
            send_reply(event.reply_token, [TextMessage(text="目前的口袋名單空空如也喔！快去加入餐廳吧。")], include_menu=True)
        else:
            # 調整：同時發送「圖卡」與「帶有主選單的提示文字」
            carousel_msg = get_carousel_list_message(user_list, page=1)
            hint_msg = TextMessage(text="以上是您的口袋名單，您也可以透過下方選單繼續操作：")
            send_reply(event.reply_token, [carousel_msg, hint_msg], include_menu=True)
    else:
        send_reply(event.reply_token, [TextMessage(text="請點選下方選單來操作喔！")], include_menu=True)


@line_handler.add(PostbackEvent)
def handle_postback(event):
    postback_data = event.postback.data
    user_id = event.source.user_id
    
    params = dict(urllib.parse.parse_qsl(postback_data))
    menu_action = params.get('menu_action')
    action = params.get('action')
    
    # ================= 1. 處理 Quick Reply 與分頁導向的選單動作 =================
    if menu_action == 'click_add':
        set_user_state(user_id, 'WAIT_FOR_ADD')
        send_reply(event.reply_token, [TextMessage(text="請直接輸入你想加入的餐廳名稱：")])
        return
        
    elif menu_action == 'click_list':
        page = int(params.get('page', 1))
        user_list = get_user_pocket_list(user_id)
        if not user_list:
            reply_text = "目前的口袋名單空空如也喔！快去加入餐廳吧。"
            send_reply(event.reply_token, [TextMessage(text=reply_text)], include_menu=True)
        else:
            # 調整：這裡也是點選看名單/下一頁時，同時附帶圖卡與選單提示
            carousel_msg = get_carousel_list_message(user_list, page=page)
            hint_msg = TextMessage(text="可以滑動查看名單，或是點選下方功能：")
            send_reply(event.reply_token, [carousel_msg, hint_msg], include_menu=True)
        return

    # ================= 2. 處理 Buttons / Carousel Template 的動作 =================
    if action == 'add':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        if is_restaurant_exist(user_id, restaurant_name):
            reply_text = f"這張卡片失效囉！「{restaurant_name}」已經在名單內了。"
        else:
            add_restaurant(user_id, restaurant_name)
            reply_text = f"已成功將「{restaurant_name}」寫入口袋名單！"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], include_menu=True)
        
    elif action == 'cancel':
        send_reply(event.reply_token, [TextMessage(text="好的，已取消加入。")], include_menu=True)

    # 新增：點擊圖卡刪除後，跳出二次確認視窗
    elif action == 'ask_delete':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        buttons_template = ButtonsTemplate(
            title="確認刪除",
            text=f"你確定要將「{restaurant_name}」從口袋名單中移除嗎？",
            actions=[
                PostbackAction(
                    label="確認刪除",
                    data=f"action=delete_confirm&name={urllib.parse.quote(restaurant_name)}",
                    displayText=f"確認刪除 {restaurant_name}"
                ),
                PostbackAction(
                    label="取消",
                    data="action=delete_cancel",
                    displayText="取消刪除"
                )
            ]
        )
        send_reply(event.reply_token, [TemplateMessage(alt_text="請確認是否刪除餐廳", template=buttons_template)])

    elif action == 'delete_confirm':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        is_deleted = delete_restaurant(user_id, restaurant_name)
        if is_deleted:
            reply_text = f"已將「{restaurant_name}」從你的口袋名單中移除！"
        else:
            reply_text = f"這張卡片失效囉！名單內已無「{restaurant_name}」"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], include_menu=True)
        
    elif action == 'delete_cancel':
        send_reply(event.reply_token, [TextMessage(text="好的，已取消刪除，保留餐廳。")], include_menu=True)


def send_reply(reply_token, messages, include_menu=False):
    if include_menu and messages and isinstance(messages[-1], TextMessage):
        messages[-1].quick_reply = get_main_quick_reply()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages
            )
        )

init_db()