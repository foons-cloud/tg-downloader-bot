import os
import sqlite3
from flask import Flask, render_template_string

DB_PATH = os.environ.get("DB_PATH", "/app/data/usage.db")
PORT = int(os.environ.get("PORT", 8080))

app = Flask(__name__)

PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8">
  <title>داشبورد البوت</title>
  <style>
    body { font-family: Tahoma, Arial, sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:24px; }
    h1 { font-size:20px; margin-bottom:4px; }
    .stats { display:flex; gap:16px; margin:16px 0 24px 0; flex-wrap:wrap; }
    .card { background:#1a1d23; border-radius:10px; padding:16px 20px; min-width:140px; }
    .card .num { font-size:26px; font-weight:bold; color:#4ade80; }
    .card .label { font-size:13px; color:#9ca3af; margin-top:4px; }
    table { width:100%; border-collapse:collapse; background:#1a1d23; border-radius:10px; overflow:hidden; font-size:13px; }
    th, td { padding:10px 12px; text-align:right; border-bottom:1px solid #2a2d35; }
    th { background:#21242b; color:#9ca3af; font-weight:600; }
    .success { color:#4ade80; }
    .failed { color:#f87171; }
    .empty { padding:40px; text-align:center; color:#6b7280; }
  </style>
</head>
<body>
  <h1>📊 داشبورد بوت التحميل</h1>
  <div class="stats">
    <div class="card"><div class="num">{{ total }}</div><div class="label">إجمالي الطلبات</div></div>
    <div class="card"><div class="num">{{ success_count }}</div><div class="label">نجح</div></div>
    <div class="card"><div class="num">{{ failed_count }}</div><div class="label">فشل</div></div>
    <div class="card"><div class="num">{{ unique_users }}</div><div class="label">مستخدمين فريدين</div></div>
  </div>

  {% if rows %}
  <table>
    <tr>
      <th>الوقت</th>
      <th>المستخدم</th>
      <th>الموقع</th>
      <th>الحالة</th>
      <th>تفاصيل</th>
    </tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.created_at }}</td>
      <td>{{ r.username or r.first_name or r.user_id }}</td>
      <td>{{ r.domain }}</td>
      <td class="{{ 'success' if r.status == 'success' else 'failed' }}">
        {{ '✅ نجح' if r.status == 'success' else '❌ فشل' }}
      </td>
      <td>{{ r.error_message or '-' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="empty">لا يوجد أي استخدام مسجل لحد الآن.</div>
  {% endif %}
</body>
</html>
"""


def get_data():
    if not os.path.exists(DB_PATH):
        return [], 0, 0, 0, 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM downloads ORDER BY id DESC LIMIT 200"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    success_count = conn.execute(
        "SELECT COUNT(*) FROM downloads WHERE status='success'"
    ).fetchone()[0]
    failed_count = conn.execute(
        "SELECT COUNT(*) FROM downloads WHERE status='failed'"
    ).fetchone()[0]
    unique_users = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM downloads"
    ).fetchone()[0]
    conn.close()
    return rows, total, success_count, failed_count, unique_users


@app.route("/")
def dashboard():
    rows, total, success_count, failed_count, unique_users = get_data()
    return render_template_string(
        PAGE_TEMPLATE,
        rows=rows,
        total=total,
        success_count=success_count,
        failed_count=failed_count,
        unique_users=unique_users,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
