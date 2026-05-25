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
# 直接讀取雲端連線字串
DB_URL = os.environ.get('POSTGRES_URL') 

configuration = Configuration(access_token=os.getenv('CHANNEL_ACCESS_TOKEN'))
line_handler = WebhookHandler(os.getenv('CHANNEL_SECRET'))


# 當你在 Vercel 點選 Neon 整合後，Vercel 會自動把連線字串注入到環境變數 'POSTGRES_URL' 中

# ==================== Neon PostgreSQL 資料庫操作邏輯 ====================

def get_db_connection():
    """建立並回傳 Neon 雲端資料庫的連線"""
    # 這裡加入防禦，萬一本機測試沒設定環境變數，會給出明確提示
    if not DB_URL:
        raise ValueError("環境變數 POSTGRES_URL 未設定！請確認 Vercel 整合或本機 .env 設定。")
    
    # Neon 的連線字串開頭可能是 postgres://，psycopg2 完美支援
    return psycopg2.connect(DB_URL)

def init_db():
    """初始化雲端資料庫，確保資料表存在"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. 建立口袋名單表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pocket_list (
            user_id TEXT,
            restaurant_name TEXT,
            PRIMARY KEY (user_id, restaurant_name)
        )
    ''')
    
    # 2. 建立使用者狀態表
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
    """檢查該使用者是否已經將該餐廳加入口袋名單 (佔位符改為 %s)"""
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
    """將餐廳寫入該使用者的清單中 (改用 PostgreSQL 的 ON CONFLICT 語法)"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # PostgreSQL 的標準作法：若主鍵衝突則什麼都不做 (DO NOTHING)
        cursor.execute('''
            INSERT INTO pocket_list (user_id, restaurant_name) 
            VALUES (%s, %s)
            ON CONFLICT (user_id, restaurant_name) DO NOTHING
        ''', (user_id, restaurant_name))
        
        # cursor.rowcount 可以知道有沒有成功塞入資料，如果等於 0 代表衝突跳過了
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
    """將特定使用者的某間餐廳從資料庫刪除"""
    init_db()  # 防禦性設計
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
    """取得特定使用者的名單"""
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
    """設定使用者的狀態 (改用 PostgreSQL 的 ON CONFLICT ... DO UPDATE 語法)"""
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
    """取得使用者目前狀態"""
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
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'

@line_handler.add(FollowEvent)
def handle_follow(event):
    # 新加入好友時，主動發送歡迎訊息並帶上 Quick Reply 選單
    send_reply(event.reply_token, [TextMessage(text="嗨！我是你的口袋名單助手，請選擇你想執行的功能：")], include_menu=True)


def get_main_quick_reply():
    """產生主功能的 Quick Reply 選單 (使用 Postback 隱藏狀態切換)"""
    return QuickReply(
        items=[
            QuickReplyItem(
                action=PostbackAction(label="➕ 加入餐廳", data="menu_action=click_add", displayText="點擊了加入餐廳")
            ),
            QuickReplyItem(
                action=PostbackAction(label="📋 我的口袋名單", data="menu_action=click_list", displayText="查看口袋名單")
            ),
            QuickReplyItem(
                action=PostbackAction(label="❌ 刪除餐廳", data="menu_action=click_delete", displayText="點擊了刪除餐廳")
            )
        ]
    )


@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id 
    
    # 檢查使用者當前的狀態
    current_state = get_user_state(user_id)

    # ------ 狀態 A：等待使用者輸入「要加入的餐廳名稱」 ------
    if current_state == 'WAIT_FOR_ADD':
        restaurant_name = user_message
        
        # 收到名稱後，先回復成 IDLE 狀態，避免下次輸入被誤判
        set_user_state(user_id, 'IDLE')
        
        # 彈出確認視窗
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
        template_message = TemplateMessage(
            alt_text="請確認是否加入口袋名單",
            template=buttons_template
        )
        send_reply(event.reply_token, [template_message])
        return

    # ------ 狀態 B：等待使用者輸入「要刪除的餐廳名稱」 ------
    elif current_state == 'WAIT_FOR_DELETE':
        restaurant_name = user_message
        set_user_state(user_id, 'IDLE')

        # 檢查名單有沒有這家店
        if not is_restaurant_exist(user_id, restaurant_name):
            reply_text = f"你的口袋名單裡本來就沒有「{restaurant_name}」喔。"
            send_reply(event.reply_token, [TextMessage(text=reply_text)], include_menu=True)
            return

        # 跳出刪除確認按鈕
        buttons_template = ButtonsTemplate(
            title="刪除口袋名單",
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
        template_message = TemplateMessage(
            alt_text="請確認是否刪除餐廳",
            template=buttons_template
        )
        send_reply(event.reply_token, [template_message])
        return

    # ------ 一般狀態 (IDLE) 底下的關鍵字相容（保留原本純文字指令，防禦用） ------
    if user_message == '我的口袋名單':
        user_list = get_user_pocket_list(user_id)
        count = len(user_list)
        if count == 0:
            reply_text = "目前的口袋名單空空如也喔！快去加入餐廳吧。"
        else:
            list_content = "\n".join([f"{i+1}. {name}" for i, name in enumerate(user_list)])
            reply_text = f"你的口袋名單目前共有 {count} 間餐廳：\n\n{list_content}"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], include_menu=True)
    
    else:
        # 如果使用者輸入了看不懂的字，貼心地彈出選單提示他
        send_reply(event.reply_token, [TextMessage(text="請點選下方選單來操作喔！")], include_menu=True)


@line_handler.add(PostbackEvent)
def handle_postback(event):
    postback_data = event.postback.data
    user_id = event.source.user_id
    
    params = dict(urllib.parse.parse_qsl(postback_data))
    menu_action = params.get('menu_action')
    action = params.get('action')
    
    # ================= 1. 處理 Quick Reply 導向的選單動作 =================
    if menu_action == 'click_add':
        set_user_state(user_id, 'WAIT_FOR_ADD')
        send_reply(event.reply_token, [TextMessage(text="請直接輸入你想加入的餐廳名稱：")])
        return
        
    elif menu_action == 'click_delete':
        set_user_state(user_id, 'WAIT_FOR_DELETE')
        send_reply(event.reply_token, [TextMessage(text="請直接輸入你想刪除的餐廳名稱：")])
        return
        
    elif menu_action == 'click_list':
        # 點選查看名單，直接撈資料回覆
        user_list = get_user_pocket_list(user_id)
        count = len(user_list)
        if count == 0:
            reply_text = "目前的口袋名單空空如也喔！快去加入餐廳吧。"
        else:
            list_content = "\n".join([f"{i+1}. {name}" for i, name in enumerate(user_list)])
            reply_text = f"你的口袋名單目前共有 {count} 間餐廳：\n\n{list_content}"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], include_menu=True)
        return

    # ================= 2. 處理 Buttons Template 的確認/取消動作 =================
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

    elif action == 'delete_confirm':
        restaurant_name = urllib.parse.unquote(params.get('name', ''))
        is_deleted = delete_restaurant(user_id, restaurant_name)
        if is_deleted:
            reply_text = f"已將「{restaurant_name}」從你的口袋名單中移除！"
        else:
            reply_text = f"這張卡片失效囉！名單內已無「{restaurant_name}」。"
        send_reply(event.reply_token, [TextMessage(text=reply_text)], include_menu=True)
        
    elif action == 'delete_cancel':
        send_reply(event.reply_token, [TextMessage(text="好的，已取消刪除，保留餐廳。")], include_menu=True)


def send_reply(reply_token, messages, include_menu=False):
    # 控制是否在最後一句話附帶選單
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