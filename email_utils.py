import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.message import EmailMessage
from db import get_db

SENDER_EMAIL = "ntubimd55555@gmail.com"
SENDER_PASSWORD = "sdcy qael qrcm cqqw"

# 通用寄信函式：支援多收件人、純文字與 HTML
def send_email_notification(to_email, subject, body):
    msg = MIMEMultipart("alternative")
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject

    # 純文字版本
    text = body

    # ✅ 在 f-string 外先處理換行 -> <br>
    html_body = body.replace("\n", "<br>")

    # ✅ 正確 f-string 多行 HTML
    html = f"""
    <html>
      <body>
        <p>{html_body}</p>
      </body>
    </html>
    """

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        print(f"✅ 信寄到：{to_email}")
        return True
    except Exception as e:
        print(f"❌ 郵件寄送失敗：{e}")
        return False

# 邀請信
def send_invite_email(to_email, org_name):
    subject = f"📬 您已被邀請加入組織：{org_name}"
    body = f"您好，\n\n您已被邀請加入「{org_name}」，請登入系統確認。"
    return send_email_notification(to_email, subject, body)

# 會議通知信
def send_meeting_email(emails, meeting_title, meeting_date, org_name):
    subject = f"📢【{org_name}】會議通知：{meeting_title}"
    body = f"""您好，

您已被邀請參與由「{org_name}」發起的會議：

🔷 主題：{meeting_title}
🔷 時間：{meeting_date}

請準時參加，謝謝！
—— 會議寶系統自動通知"""

    for email in emails:
        send_email_notification(email, subject, body)


#忘記密碼
def get_user_by_email(email):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    sql = "SELECT * FROM users WHERE email = %s"
    cursor.execute(sql, (email,))
    return cursor.fetchone()
