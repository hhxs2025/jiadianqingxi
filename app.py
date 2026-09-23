# -*- coding: utf-8 -*-
"""
家电清洗服务预约 · v1.0.3
- 管理员：管师傅、改密码、改邮箱、看全局、派单、删除订单、改状态、导出、数据看板、系统设置
- 师傅：姓名 + 密码登录，处理自己的订单，有专属服务码
- 客户：手机号 + 服务码（选填）登录，预约下单

v1.0.6 更新：
- 修复：已完成/已取消的订单不允许改派（避免师傅收到已结束的单）
- 新增：管理员可删除订单，连同照片/签名一并清理
"""
import os
import re
import io
import csv
import json
import time
import uuid
import hmac
import base64
import random
import smtplib
import sqlite3
import threading
from datetime import datetime, date, timedelta
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr

from werkzeug.security import generate_password_hash, check_password_hash
from flask import (Flask, g, request, session, jsonify, render_template,
                   redirect, url_for, send_from_directory)

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from alibabacloud_dysmsapi20170525.client import Client as AliyunSmsClient
    from alibabacloud_tea_openapi import models as ali_openapi_models
    from alibabacloud_dysmsapi20170525 import models as ali_sms_models
    HAS_ALIYUN_SMS = True
except ImportError:
    HAS_ALIYUN_SMS = False

try:
    from tencentcloud.common import credential as tc_credential
    from tencentcloud.sms.v20210111 import sms_client as tc_sms_client
    from tencentcloud.sms.v20210111 import models as tc_sms_models
    HAS_TENCENT_SMS = True
except ImportError:
    HAS_TENCENT_SMS = False


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
DB_PATH = os.path.join(DATA_DIR, 'data.db')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

PHOTO_MIN_INTERVAL = int(os.environ.get('PHOTO_MIN_INTERVAL', '180'))

CLEAN_OBJECTS = ['油烟机', '冰箱', '空调', '洗衣机', '热水器', '太阳能', '水管']
SERVICE_TYPES = ['家电清洗', '管道疏通']
TIME_SLOTS = ['上午 08:00-12:00', '下午 12:00-18:00', '晚上 18:00-21:00']
PHONE_RE = re.compile(r'^1[3-9]\d{9}$')
ORDER_STATUS = ['待接单', '已接单', '服务中', '已完成', '已取消']

NEXT_STATE = {
    '待接单': ['已接单', '已取消'],
    '已接单': ['服务中', '已取消'],
    '服务中': ['已完成', '已取消'],
    '已完成': [],
    '已取消': ['待接单'],
}

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

SERVICE_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
DEFAULT_ENGINEER_PASSWORD = '123456'


# =================================================================== #
#                              数据库                                 #
# =================================================================== #
def get_db():
    if '_db' not in g:
        g._db = sqlite3.connect(DB_PATH)
        g._db.row_factory = sqlite3.Row
        g._db.execute('PRAGMA journal_mode=WAL')
        g._db.execute('PRAGMA busy_timeout=5000')
    return g._db


@app.teardown_appcontext
def _close_db(exc):
    db = g.pop('_db', None)
    if db is not None:
        db.close()


def _gen_service_code_with_db(db):
    for _ in range(50):
        code = ''.join(random.choices(SERVICE_CODE_ALPHABET, k=8))
        if not db.execute('SELECT 1 FROM engineers WHERE service_code = ?', (code,)).fetchone():
            return code
    return None


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no      TEXT UNIQUE NOT NULL,
            user_key      TEXT NOT NULL,
            contact_phone TEXT NOT NULL,
            service_type  TEXT NOT NULL,
            clean_object  TEXT,
            time_slot     TEXT,
            book_date     TEXT NOT NULL,
            address       TEXT NOT NULL,
            remark        TEXT,
            photo1        TEXT,
            photo2        TEXT,
            photo_gap     INTEGER,
            signature     TEXT,
            engineer_id   INTEGER,
            status        TEXT DEFAULT '待接单',
            created_at    TEXT NOT NULL
        )""")

        db.execute("""
        CREATE TABLE IF NOT EXISTS engineers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            code          TEXT,
            name          TEXT NOT NULL,
            phone         TEXT,
            note          TEXT,
            email         TEXT,
            service_code  TEXT,
            password_hash TEXT,
            enabled       INTEGER DEFAULT 1,
            created_at    TEXT NOT NULL
        )""")

        db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )""")

        def cols(t):
            return [r[1] for r in db.execute("PRAGMA table_info(%s)" % t).fetchall()]

        ocols = cols('orders')
        if 'signature' not in ocols:
            db.execute("ALTER TABLE orders ADD COLUMN signature TEXT")
        if 'time_slot' not in ocols:
            db.execute("ALTER TABLE orders ADD COLUMN time_slot TEXT")
        if 'engineer_id' not in ocols:
            db.execute("ALTER TABLE orders ADD COLUMN engineer_id INTEGER")
        if 'photo1_exif' not in ocols:
            db.execute("ALTER TABLE orders ADD COLUMN photo1_exif INTEGER")
        if 'photo2_exif' not in ocols:
            db.execute("ALTER TABLE orders ADD COLUMN photo2_exif INTEGER")
        if 'gap_source' not in ocols:
            db.execute("ALTER TABLE orders ADD COLUMN gap_source TEXT")

        ecols = cols('engineers')
        if 'service_code' not in ecols:
            db.execute("ALTER TABLE engineers ADD COLUMN service_code TEXT")
        if 'password_hash' not in ecols:
            db.execute("ALTER TABLE engineers ADD COLUMN password_hash TEXT")
        if 'email' not in ecols:
            db.execute("ALTER TABLE engineers ADD COLUMN email TEXT")

        rows = db.execute(
            "SELECT id FROM engineers WHERE service_code IS NULL OR service_code = ''"
        ).fetchall()
        for r in rows:
            code = _gen_service_code_with_db(db)
            if code:
                db.execute('UPDATE engineers SET service_code = ? WHERE id = ?',
                           (code, r[0]))

        rows = db.execute(
            "SELECT id FROM engineers WHERE password_hash IS NULL OR password_hash = ''"
        ).fetchall()
        default_hash = generate_password_hash(DEFAULT_ENGINEER_PASSWORD)
        for r in rows:
            db.execute('UPDATE engineers SET password_hash = ? WHERE id = ?',
                       (default_hash, r[0]))

        db.commit()


init_db()


# =================================================================== #
#                        settings 表读写                              #
# =================================================================== #
def get_setting(key, default=''):
    db = get_db()
    row = db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    if row is not None and row['value'] not in (None, ''):
        return row['value']
    env_key = key.upper()
    if env_key in os.environ:
        return os.environ[env_key]
    return default


def set_setting(key, value):
    db = get_db()
    db.execute(
        'INSERT INTO settings (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, str(value if value is not None else ''))
    )
    db.commit()


def get_all_settings():
    db = get_db()
    return {r['key']: r['value'] for r in
            db.execute('SELECT key, value FROM settings').fetchall()}


# =================================================================== #
#                          会话 / 权限装饰器                          #
# =================================================================== #
def current_user():
    return session.get('user')


def current_engineer():
    return session.get('engineer')


def login_required_page(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not current_user():
            return redirect(url_for('login', next=request.path))
        return f(*a, **k)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get('is_admin'):
            if request.path.startswith('/admin/api/'):
                return jsonify(ok=False, msg='登录已过期，请重新登录管理员后台'), 401
            return redirect(url_for('admin_login', next=request.path))
        return f(*a, **k)
    return wrapper


def engineer_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        eng = session.get('engineer')
        if not eng:
            if request.path.startswith('/engineer/api/'):
                return jsonify(ok=False, msg='登录已过期，请重新登录'), 401
            return redirect(url_for('engineer_login', next=request.path))
        db = get_db()
        row = db.execute('SELECT * FROM engineers WHERE id = ?', (eng['id'],)).fetchone()
        if not row or not row['enabled']:
            session.pop('engineer', None)
            if request.path.startswith('/engineer/api/'):
                return jsonify(ok=False, msg='账号已被停用，请联系管理员'), 401
            return redirect(url_for('engineer_login'))
        return f(*a, **k)
    return wrapper


# =================================================================== #
#                              工具函数                               #
# =================================================================== #
def sniff_image(d):
    if d[:3] == b'\xff\xd8\xff': return '.jpg'
    if d[:8] == b'\x89PNG\r\n\x1a\n': return '.png'
    if d[:4] == b'RIFF' and d[8:12] == b'WEBP': return '.webp'
    return None


def read_exif_datetime(file_bytes):
    if not HAS_PIL:
        return None
    try:
        img = Image.open(io.BytesIO(file_bytes))
        exif = img._getexif()
        if not exif:
            return None
        for tag_id, value in exif.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == 'DateTimeOriginal':
                try:
                    dt = datetime.strptime(str(value), '%Y:%m:%d %H:%M:%S')
                    return dt.timestamp()
                except (ValueError, TypeError):
                    return None
        return None
    except Exception:
        return None


def _calc_photo_gap(photo_a, photo_b):
    exif_a = photo_a.get('exif_ts')
    exif_b = photo_b.get('exif_ts')
    if exif_a and exif_b:
        gap = int(round(exif_b - exif_a))
        if gap >= 0:
            return gap, 'exif'
    gap = int(round(photo_b['ts'] - photo_a['ts']))
    return gap, 'upload'


def gen_engineer_code():
    db = get_db()
    for _ in range(50):
        code = ''.join(random.choices('0123456789', k=6))
        if not db.execute('SELECT 1 FROM engineers WHERE code = ?', (code,)).fetchone():
            return code
    return None


def gen_service_code():
    db = get_db()
    return _gen_service_code_with_db(db)


def find_engineer_by_service_code(code):
    if not code:
        return True, '', None
    db = get_db()
    row = db.execute('SELECT * FROM engineers WHERE service_code = ?', (code,)).fetchone()
    if not row:
        return False, '服务码无效，请检查或留空直接预约', None
    if not row['enabled']:
        return False, '该服务码已停用，请留空直接预约或联系客服', None
    return True, '', row


# =================================================================== #
#                          邮件 + 短信通知                             #
# =================================================================== #
def _public_base():
    pu = get_setting('public_url', '').strip()
    if pu:
        return pu.rstrip('/')
    return request.host_url.rstrip('/')


def _send_email(to_list, subject, html):
    if get_setting('email_enabled', '1') != '1':
        return False, '邮件通知未启用'
    host = get_setting('smtp_host', 'smtp.qq.com')
    try:
        port = int(get_setting('smtp_port', '465') or 465)
    except ValueError:
        port = 465
    user = get_setting('smtp_user')
    password = get_setting('smtp_pass')
    if not (host and user and password and to_list):
        return False, '邮件配置不完整'
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = formataddr(('家电清洗预约', user))
        msg['To'] = ', '.join(to_list)
        msg['Subject'] = Header(subject, 'utf-8')
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        with smtplib.SMTP_SSL(host, port, timeout=15) as s:
            s.login(user, password)
            s.sendmail(user, to_list, msg.as_string())
        return True, '发送成功'
    except Exception as e:
        app.logger.warning('邮件发送失败: %s', e)
        return False, str(e)


def _build_new_order_email(order_row, base, audience='admin'):
    """
    构造新订单的 HTML 邮件正文。

    audience 控制按钮链接：
    - audience='admin'    → 按钮指向 /admin/orders（管理员后台）
    - audience='engineer' → 按钮指向 /engineer（师傅工作台）
    """
    eng_name = '未指定'
    if order_row['engineer_id']:
        db = get_db()
        er = db.execute('SELECT name FROM engineers WHERE id = ?',
                        (order_row['engineer_id'],)).fetchone()
        if er:
            eng_name = er['name']

    photos_html = ''
    photo_count = 0
    for idx, key in enumerate(['photo1', 'photo2'], 1):
        fn = order_row[key]
        if fn:
            photo_count += 1
            photos_html += (
                '<div style="display:inline-block;margin:0 10px 10px 0;">'
                '<div style="font-size:12px;color:#888;margin-bottom:4px;">照片 %d</div>'
                '<img src="%s/uploads/%s" '
                'style="width:180px;height:180px;object-fit:cover;'
                'border-radius:8px;border:1px solid #eee;">'
                '</div>'
            ) % (idx, base, fn)

    sign_html = ''
    if order_row['signature']:
        sign_html = (
            '<tr><td style="padding:8px 0;color:#888;width:96px;">手写签名</td>'
            '<td><img src="%s/uploads/%s" '
            'style="height:80px;background:#fff;border:1px solid #eee;'
            'border-radius:6px;padding:4px;"></td></tr>'
        ) % (base, order_row['signature'])

    gap_row = ''
    if order_row['photo_gap']:
        gap_source = order_row['gap_source'] or 'upload'
        gap_note = 'EXIF 拍摄时间' if gap_source == 'exif' else '上传时间'
        gap_row = ('<tr><td style="padding:8px 0;color:#888;">照片间隔</td>'
                   '<td>%d 秒（%s）</td></tr>') % (order_row['photo_gap'], gap_note)

    photos_block = ''
    if photo_count > 0:
        photos_block = (
            '<div style="margin-top:20px;">'
            '<div style="color:#888;font-size:13px;margin-bottom:8px;">运行状态照片</div>'
            '%s'
            '</div>'
        ) % photos_html

    # 按身份选择按钮
    if audience == 'engineer':
        btn_url = base + '/engineer'
        btn_text = '打开工作台处理订单'
    else:
        btn_url = base + '/admin/orders'
        btn_text = '进入后台查看'

    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f4f6fb;font-family:-apple-system,'PingFang SC',Arial,sans-serif;">
<div style="max-width:640px;margin:0 auto;background:#fff;border-radius:14px;padding:24px;box-shadow:0 4px 16px rgba(0,0,0,.06);">
<h2 style="margin:0 0 4px;color:#2f7cf6;font-size:18px;">🧼 新预约单</h2>
<p style="margin:0 0 18px;color:#8b95a7;font-size:13px;">订单号 %s</p>
<table style="width:100%%;border-collapse:collapse;font-size:14px;">
<tr><td style="padding:8px 0;color:#888;width:96px;">服务类型</td><td>%s %s</td></tr>
<tr><td style="padding:8px 0;color:#888;">预约时间</td><td>%s %s</td></tr>
<tr><td style="padding:8px 0;color:#888;">联系电话</td><td><b>%s</b></td></tr>
<tr><td style="padding:8px 0;color:#888;">地址</td><td>%s</td></tr>
<tr><td style="padding:8px 0;color:#888;">备注</td><td>%s</td></tr>
%s
<tr><td style="padding:8px 0;color:#888;">接单师傅</td><td><b>%s</b></td></tr>
%s
</table>
%s
<div style="margin-top:20px;text-align:center;">
<a href="%s" style="display:inline-block;background:#2f7cf6;color:#fff;
   text-decoration:none;padding:12px 28px;border-radius:10px;font-size:14px;font-weight:600;">
   %s
</a>
</div>
</div></body></html>""" % (
        order_row['order_no'],
        order_row['service_type'],
        ('· ' + order_row['clean_object']) if order_row['clean_object'] else '',
        order_row['book_date'],
        ('· ' + order_row['time_slot']) if order_row['time_slot'] else '',
        order_row['contact_phone'],
        order_row['address'],
        order_row['remark'] or '（无）',
        sign_html,
        eng_name,
        gap_row,
        photos_block,
        btn_url,
        btn_text,
    )


# =================================================================== #
#                              短信发送                               #
# =================================================================== #
def _send_sms_aliyun(phone, template_code, params):
    if not HAS_ALIYUN_SMS:
        return False, '阿里云短信 SDK 未安装'
    ak = get_setting('sms_aliyun_access_key_id')
    sk = get_setting('sms_aliyun_access_key_secret')
    sign = get_setting('sms_sign')
    if not (ak and sk and sign):
        return False, '阿里云短信配置不完整'
    try:
        cfg = ali_openapi_models.Config(access_key_id=ak, access_key_secret=sk)
        cfg.endpoint = 'dysmsapi.aliyuncs.com'
        client = AliyunSmsClient(cfg)
        param_json = {str(i + 1): str(p) for i, p in enumerate(params)}
        req = ali_sms_models.SendSmsRequest(
            phone_numbers=phone,
            sign_name=sign,
            template_code=template_code,
            template_param=json.dumps(param_json, ensure_ascii=False),
        )
        resp = client.send_sms(req)
        if resp.body and resp.body.code == 'OK':
            return True, '发送成功'
        return False, (resp.body.message if resp.body else '发送失败')
    except Exception as e:
        return False, str(e)


def _send_sms_tencent(phone, template_id, params):
    if not HAS_TENCENT_SMS:
        return False, '腾讯云短信 SDK 未安装'
    sid = get_setting('sms_tencent_secret_id')
    skey = get_setting('sms_tencent_secret_key')
    app_id = get_setting('sms_tencent_sdk_app_id')
    sign = get_setting('sms_sign')
    if not (sid and skey and app_id and sign):
        return False, '腾讯云短信配置不完整'
    try:
        cred = tc_credential.Credential(sid, skey)
        client = tc_sms_client.SmsClient(cred, 'ap-guangzhou')
        if not phone.startswith('+'):
            phone = '+86' + phone
        req = tc_sms_models.SendSmsRequest()
        req.SmsSdkAppId = app_id
        req.SignName = sign
        req.TemplateId = template_id
        req.PhoneNumberSet = [phone]
        req.TemplateParamSet = [str(p) for p in params]
        resp = client.SendSms(req)
        if resp.SendStatusSet and resp.SendStatusSet[0].Code == 'Ok':
            return True, '发送成功'
        msg = resp.SendStatusSet[0].Message if resp.SendStatusSet else '发送失败'
        return False, msg
    except Exception as e:
        return False, str(e)


def send_sms(phone, template_id, params):
    if not phone:
        return False, '手机号为空'
    if get_setting('sms_enabled', '0') != '1':
        return False, '短信通知未启用'
    if not template_id:
        return False, '未配置短信模板'
    provider = get_setting('sms_provider', 'aliyun')
    if provider == 'aliyun':
        return _send_sms_aliyun(phone, template_id, params)
    if provider == 'tencent':
        return _send_sms_tencent(phone, template_id, params)
    return False, '未知的短信服务商：' + provider


def get_sms_template(event):
    provider = get_setting('sms_provider', 'aliyun')
    return get_setting('sms_%s_template_%s' % (provider, event))


def notify_engineer_new_order(order_row):
    if not order_row['engineer_id']:
        return
    db = get_db()
    eng = db.execute('SELECT * FROM engineers WHERE id = ?',
                     (order_row['engineer_id'],)).fetchone()
    if not eng or not eng['phone']:
        return
    tpl = get_sms_template('order')
    if not tpl:
        return
    phone_tail = order_row['contact_phone'][-4:] if order_row['contact_phone'] else '****'
    time_str = order_row['book_date']
    if order_row['time_slot']:
        slot_short = order_row['time_slot'].split(' ')[0]
        time_str = '%s %s' % (order_row['book_date'][5:], slot_short)
    try:
        send_sms(eng['phone'], tpl, [phone_tail, time_str])
    except Exception as e:
        app.logger.warning('师傅短信通知失败: %s', e)


def notify_customer_status_change(order_row, new_status):
    tpl = None
    params = []

    if new_status == '已接单':
        tpl = get_sms_template('accepted')
        if order_row['engineer_id']:
            db = get_db()
            er = db.execute('SELECT name FROM engineers WHERE id = ?',
                            (order_row['engineer_id'],)).fetchone()
            params = [er['name'] if er else '师傅']
        else:
            params = ['师傅']
    elif new_status == '已完成':
        tpl = get_sms_template('done')
    else:
        return

    if not tpl:
        return
    try:
        send_sms(order_row['contact_phone'], tpl, params)
    except Exception as e:
        app.logger.warning('客户短信通知失败: %s', e)


def _notify_engineer_assignment(order_row):
    """
    管理员派单后，通知该订单归属的师傅（邮件 + 短信）。
    邮件按钮指向师傅工作台 /engineer
    """
    if not order_row['engineer_id']:
        return

    db = get_db()
    eng = db.execute('SELECT * FROM engineers WHERE id = ?',
                     (order_row['engineer_id'],)).fetchone()
    if not eng:
        return

    # 邮件（audience='engineer'）
    if eng['email']:
        base = _public_base()
        html = _build_new_order_email(order_row, base, audience='engineer')
        subject = '【您有新预约】%s %s' % (
            order_row['service_type'],
            order_row['book_date'],
        )
        _send_email([eng['email']], subject, html)

    # 短信
    if eng['phone']:
        tpl = get_sms_template('order')
        if tpl:
            phone_tail = order_row['contact_phone'][-4:] if order_row['contact_phone'] else '****'
            time_str = order_row['book_date']
            if order_row['time_slot']:
                slot_short = order_row['time_slot'].split(' ')[0]
                time_str = '%s %s' % (order_row['book_date'][5:], slot_short)
            try:
                send_sms(eng['phone'], tpl, [phone_tail, time_str])
            except Exception as e:
                app.logger.warning('派单短信通知失败: %s', e)


def notify_new_order(order_row):
    """
    客户下单：
    - 管理员收件邮箱（smtp_to）必收一封（按钮 → /admin/orders）
    - 按订单归属 engineer_id，单独发给对应师傅（按钮 → /engineer）
    - 师傅邮箱与管理员邮箱重复时不重复发
    - 附带短信通知
    """
    # 1) 管理员邮箱（必收）
    admin_str = get_setting('smtp_to', '')
    admin_recipients = [x.strip() for x in admin_str.split(',') if x.strip()]
    sent_addresses = set(a.lower() for a in admin_recipients)

    if admin_recipients:
        base = _public_base()
        html_admin = _build_new_order_email(order_row, base, audience='admin')
        subject_admin = '【新预约】%s %s %s' % (
            order_row['service_type'],
            order_row['clean_object'] or '',
            order_row['book_date'],
        )
        _send_email(admin_recipients, subject_admin, html_admin)

    # 2) 归属师傅
    if order_row['engineer_id']:
        db = get_db()
        eng = db.execute('SELECT name, email FROM engineers WHERE id = ?',
                         (order_row['engineer_id'],)).fetchone()
        if eng and eng['email']:
            eng_email = eng['email'].strip()
            if eng_email.lower() not in sent_addresses:
                base = _public_base()
                html_eng = _build_new_order_email(order_row, base, audience='engineer')
                subject_eng = '【您有新预约】%s %s' % (
                    order_row['service_type'],
                    order_row['book_date'],
                )
                _send_email([eng_email], subject_eng, html_eng)

    # 3) 短信
    notify_engineer_new_order(order_row)


# =================================================================== #
#                         孤儿文件清理                                #
# =================================================================== #
def cleanup_orphan_files():
    db = get_db()
    used = set()
    for r in db.execute('SELECT photo1, photo2, signature FROM orders').fetchall():
        for k in ('photo1', 'photo2', 'signature'):
            if r[k]:
                used.add(r[k])

    deleted = 0
    freed = 0
    try:
        for fn in os.listdir(UPLOAD_DIR):
            if fn in used:
                continue
            path = os.path.join(UPLOAD_DIR, fn)
            if not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
                os.remove(path)
                deleted += 1
                freed += size
            except OSError:
                pass
    except FileNotFoundError:
        pass
    return deleted, freed


def _cleanup_worker():
    while True:
        time.sleep(24 * 3600)
        try:
            with app.app_context():
                d, f = cleanup_orphan_files()
                if d:
                    app.logger.info('自动清理孤儿文件：%d 个，%d 字节', d, f)
        except Exception as e:
            app.logger.warning('自动清理失败：%s', e)


_cleanup_thread = threading.Thread(target=_cleanup_worker, daemon=True)
_cleanup_thread.start()


# =================================================================== #
#                            页面：入口                               #
# =================================================================== #
@app.route('/')
def index():
    if current_user():
        return redirect(url_for('booking'))
    if session.get('engineer'):
        return redirect(url_for('engineer_home'))
    if session.get('is_admin'):
        return redirect(url_for('admin_engineers'))
    return redirect(url_for('login'))


@app.route('/login')
def login():
    if current_user():
        return redirect(url_for('booking'))
    return render_template('login.html')


@app.route('/booking')
@login_required_page
def booking():
    eng_name = ''
    eng_id = current_user().get('engineer_id')
    if eng_id:
        db = get_db()
        row = db.execute('SELECT name FROM engineers WHERE id = ?', (eng_id,)).fetchone()
        if row:
            eng_name = row['name']

    return render_template(
        'booking.html',
        user=current_user(),
        engineer_name=eng_name,
        service_types=SERVICE_TYPES,
        clean_objects=CLEAN_OBJECTS,
        time_slots=TIME_SLOTS,
        min_interval=PHOTO_MIN_INTERVAL,
        today=date.today().isoformat(),
    )


@app.route('/orders')
@login_required_page
def orders():
    db = get_db()
    rows = db.execute(
        'SELECT * FROM orders WHERE user_key = ? ORDER BY id DESC',
        (current_user()['key'],)
    ).fetchall()
    return render_template('orders.html', orders=rows,
                           new_order=request.args.get('new', ''))


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# =================================================================== #
#                          客户端登录 API                             #
# =================================================================== #
@app.post('/api/login')
def api_login():
    data = request.get_json(silent=True) or {}
    phone = (data.get('phone') or '').strip()
    code = (data.get('code') or '').strip().upper()

    if not PHONE_RE.match(phone):
        return jsonify(ok=False, msg='手机号格式不正确'), 400

    ok, msg, eng = find_engineer_by_service_code(code)
    if not ok:
        return jsonify(ok=False, msg=msg), 400

    session.clear()
    session['user'] = {
        'key': 'p:' + phone,
        'phone': phone,
        'channel': 'service_code' if eng else 'open',
        'engineer_id': eng['id'] if eng else None,
    }
    return jsonify(ok=True, redirect=url_for('booking'))


@app.post('/api/logout')
def logout():
    session.clear()
    return jsonify(ok=True, redirect=url_for('login'))


# =================================================================== #
#                          照片 / 签名 API                             #
# =================================================================== #
@app.get('/api/photo_state')
def photo_state():
    if not current_user():
        return jsonify(ok=False, msg='未登录'), 401
    photos = session.get('photos', [])
    remain = 0
    if len(photos) == 1:
        elapsed = time.time() - photos[0]['ts']
        remain = max(0, int(PHOTO_MIN_INTERVAL - elapsed + 0.999))
    return jsonify(
        ok=True, min_interval=PHOTO_MIN_INTERVAL, remain=remain,
        photos=[{'url': url_for('uploaded_file', filename=p['name']),
                 'ts': p['ts']} for p in photos],
    )


@app.post('/api/upload_photo')
def upload_photo():
    if not current_user():
        return jsonify(ok=False, msg='请先登录'), 401

    f = request.files.get('photo')
    if not f:
        return jsonify(ok=False, msg='没有收到照片'), 400

    data = f.read()
    if not data:
        return jsonify(ok=False, msg='照片内容为空'), 400
    if len(data) > 8 * 1024 * 1024:
        return jsonify(ok=False, msg='单张照片不能超过 8MB'), 400

    ext = sniff_image(data)
    if not ext:
        return jsonify(ok=False, msg='只支持 JPG / PNG / WEBP 格式的照片'), 400

    photos = list(session.get('photos', []))
    now = time.time()

    if len(photos) >= 2:
        return jsonify(ok=False, msg='最多上传两张照片，如需重传请先点击「重新上传」'), 400

    exif_ts = read_exif_datetime(data)

    if len(photos) == 1:
        gap, src = _calc_photo_gap(photos[0], {'ts': now, 'exif_ts': exif_ts})
        if gap < PHOTO_MIN_INTERVAL - 2:
            remain = PHOTO_MIN_INTERVAL - gap
            return jsonify(ok=False, remain=remain,
                msg='两张照片的拍摄时间需间隔 %d 秒，请再等待 %d 秒'
                    % (PHOTO_MIN_INTERVAL, remain)), 400

    name = uuid.uuid4().hex + ext
    with open(os.path.join(UPLOAD_DIR, name), 'wb') as fp:
        fp.write(data)

    entry = {'name': name, 'ts': now}
    if exif_ts:
        entry['exif_ts'] = exif_ts
    photos.append(entry)
    session['photos'] = photos

    if len(photos) == 1:
        remain = PHOTO_MIN_INTERVAL
        gap = None
        source = None
    else:
        remain = 0
        gap, source = _calc_photo_gap(photos[0], photos[1])

    return jsonify(ok=True, count=len(photos), remain=remain, gap=gap,
                   source=source, url=url_for('uploaded_file', filename=name))


@app.post('/api/reset_photos')
def reset_photos():
    if not current_user():
        return jsonify(ok=False, msg='请先登录'), 401
    session.pop('photos', None)
    return jsonify(ok=True)


SIGN_RE = re.compile(r'^data:image/(png|jpeg);base64,(.+)$', re.S)


@app.get('/api/signature_state')
def signature_state():
    if not current_user():
        return jsonify(ok=False, msg='未登录'), 401
    name = session.get('signature')
    return jsonify(ok=True,
                   url=url_for('uploaded_file', filename=name) if name else None)


@app.post('/api/upload_signature')
def upload_signature():
    if not current_user():
        return jsonify(ok=False, msg='请先登录'), 401

    d = request.get_json(silent=True) or {}
    data_url = d.get('image') or ''
    m = SIGN_RE.match(data_url)
    if not m:
        return jsonify(ok=False, msg='签名格式不正确'), 400

    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        return jsonify(ok=False, msg='签名解码失败'), 400

    if not raw:
        return jsonify(ok=False, msg='签名内容为空'), 400
    if len(raw) > 500 * 1024:
        return jsonify(ok=False, msg='签名图片过大'), 400

    ext = '.png' if m.group(1) == 'png' else '.jpg'
    name = 'sign_' + uuid.uuid4().hex + ext
    with open(os.path.join(UPLOAD_DIR, name), 'wb') as fp:
        fp.write(raw)

    old = session.get('signature')
    if old and old != name:
        try:
            os.remove(os.path.join(UPLOAD_DIR, old))
        except OSError:
            pass

    session['signature'] = name
    session.modified = True
    return jsonify(ok=True, url=url_for('uploaded_file', filename=name))


@app.post('/api/reset_signature')
def reset_signature():
    if not current_user():
        return jsonify(ok=False, msg='请先登录'), 401
    old = session.pop('signature', None)
    if old:
        try:
            os.remove(os.path.join(UPLOAD_DIR, old))
        except OSError:
            pass
    return jsonify(ok=True)


# =================================================================== #
#                          提交预约 API                               #
# =================================================================== #
@app.post('/api/book')
def api_book():
    user = current_user()
    if not user:
        return jsonify(ok=False, msg='请先登录'), 401

    d = request.get_json(silent=True) or {}

    service_type = (d.get('service_type') or '').strip()
    if service_type not in SERVICE_TYPES:
        return jsonify(ok=False, msg='请选择服务类型'), 400

    clean_object = (d.get('clean_object') or '').strip()
    if service_type == '家电清洗':
        if clean_object not in CLEAN_OBJECTS:
            return jsonify(ok=False, msg='请选择清洗对象'), 400
    else:
        clean_object = ''

    time_slot = (d.get('time_slot') or '').strip()
    if time_slot and time_slot not in TIME_SLOTS:
        return jsonify(ok=False, msg='请选择有效的服务时段'), 400

    book_date = (d.get('book_date') or '').strip()
    try:
        bd = datetime.strptime(book_date, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, msg='请选择预约日期'), 400
    if bd < date.today():
        return jsonify(ok=False, msg='预约日期不能早于今天'), 400

    address = (d.get('address') or '').strip()
    if len(address) < 5:
        return jsonify(ok=False, msg='请填写详细地址（精确到单元、房号）'), 400
    if len(address) > 120:
        return jsonify(ok=False, msg='地址过长，请精简后重试'), 400

    contact_phone = (d.get('contact_phone') or '').strip()
    if not PHONE_RE.match(contact_phone):
        return jsonify(ok=False, msg='请填写正确的联系电话'), 400

    remark = (d.get('remark') or '').strip()
    if len(remark) > 200:
        return jsonify(ok=False, msg='备注不能超过 200 字'), 400

    if not d.get('agreed'):
        return jsonify(ok=False, msg='请先阅读并勾选《预约必阅（服务条款）》'), 400

    signature = session.get('signature')
    if not signature:
        return jsonify(ok=False, msg='请在《预约必阅》处手写签名'), 400
    if not os.path.exists(os.path.join(UPLOAD_DIR, signature)):
        session.pop('signature', None)
        return jsonify(ok=False, msg='签名已失效，请重新签名'), 400

    photos = list(session.get('photos', []))

    if service_type == '家电清洗':
        if len(photos) != 2:
            return jsonify(ok=False, msg='请上传两张机器正常运行的照片'), 400

        gap, gap_source = _calc_photo_gap(photos[0], photos[1])
        if gap < PHOTO_MIN_INTERVAL - 2:
            return jsonify(ok=False,
                msg='两张照片的时间戳跨度不足 %d 秒，请重新拍摄' % PHOTO_MIN_INTERVAL), 400
    else:
        photos = []
        gap = 0
        gap_source = ''

    order_no = 'JX' + datetime.now().strftime('%Y%m%d%H%M%S') + str(random.randint(100, 999))
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 无服务码时，唯一在岗师傅自动分配
    engineer_id = user.get('engineer_id')
    if not engineer_id:
        db = get_db()
        active = db.execute(
            'SELECT id FROM engineers WHERE enabled = 1 ORDER BY id'
        ).fetchall()
        if len(active) == 1:
            engineer_id = active[0]['id']

    photo1_name = photos[0]['name'] if photos else None
    photo2_name = photos[1]['name'] if photos else None
    photo1_exif = int(photos[0].get('exif_ts') or 0) if photos else 0
    photo2_exif = int(photos[1].get('exif_ts') or 0) if photos else 0

    db = get_db()
    db.execute("""
        INSERT INTO orders
            (order_no, user_key, contact_phone, service_type, clean_object,
             time_slot, book_date, address, remark, photo1, photo2, photo_gap,
             photo1_exif, photo2_exif, gap_source,
             signature, engineer_id, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (order_no, user['key'], contact_phone, service_type, clean_object,
          time_slot, book_date, address, remark, photo1_name, photo2_name,
          gap, photo1_exif, photo2_exif, gap_source,
          signature, engineer_id, '待接单', now_str))
    db.commit()

    row = db.execute('SELECT * FROM orders WHERE order_no = ?', (order_no,)).fetchone()

    session.pop('photos', None)
    session.pop('signature', None)

    if row:
        try:
            notify_new_order(row)
        except Exception as e:
            app.logger.warning('新单通知异常：%s', e)

    return jsonify(ok=True, order_no=order_no, msg='预约提交成功')


# =================================================================== #
#                         管理员后台                                  #
# =================================================================== #
@app.route('/admin')
def admin_redirect():
    return redirect(url_for('admin_login'))


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'GET':
        if session.get('is_admin'):
            return redirect(url_for('admin_engineers'))
        return render_template('admin_login.html')

    pwd = (request.form.get('password') or '').strip()
    if not hmac.compare_digest(pwd, ADMIN_PASSWORD):
        return render_template('admin_login.html', error='密码错误')

    session['is_admin'] = True
    nxt = request.args.get('next') or url_for('admin_engineers')
    if not nxt.startswith('/'):
        nxt = url_for('admin_engineers')
    return redirect(nxt)


@app.post('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))


@app.route('/admin/engineers')
@admin_required
def admin_engineers():
    db = get_db()
    rows = db.execute('SELECT * FROM engineers ORDER BY id DESC').fetchall()

    stats = {}
    for r in rows:
        o = db.execute('SELECT COUNT(*) c FROM orders WHERE engineer_id = ?',
                       (r['id'],)).fetchone()['c']
        stats[r['id']] = {'orders': o}

    total_engineers = len(rows)
    active_engineers = sum(1 for r in rows if r['enabled'])
    total_orders = db.execute('SELECT COUNT(*) c FROM orders').fetchone()['c']
    today_orders = db.execute(
        "SELECT COUNT(*) c FROM orders WHERE date(created_at) = ?",
        (date.today().isoformat(),)).fetchone()['c']

    return render_template(
        'admin_engineers.html',
        engineers=rows,
        stats=stats,
        summary={
            'total_engineers': total_engineers,
            'active_engineers': active_engineers,
            'total_orders': total_orders,
            'today_orders': today_orders,
        }
    )


@app.post('/admin/api/engineers/create')
@admin_required
def admin_engineers_create():
    d = request.get_json(silent=True) or {}
    name = (d.get('name') or '').strip()[:30]
    phone = (d.get('phone') or '').strip()[:20]
    note = (d.get('note') or '').strip()[:60]
    email = (d.get('email') or '').strip()[:100]
    password = (d.get('password') or '').strip() or DEFAULT_ENGINEER_PASSWORD

    if not name:
        return jsonify(ok=False, msg='请填写师傅姓名'), 400
    if not phone:
        return jsonify(ok=False, msg='请填写师傅手机号'), 400
    if not PHONE_RE.match(phone):
        return jsonify(ok=False, msg='手机号格式不正确'), 400
    if email and '@' not in email:
        return jsonify(ok=False, msg='邮箱格式不正确'), 400
    if len(password) < 4:
        return jsonify(ok=False, msg='密码至少 4 位'), 400
    if len(password) > 50:
        return jsonify(ok=False, msg='密码不能超过 50 位'), 400

    db = get_db()
    if db.execute('SELECT 1 FROM engineers WHERE name = ?', (name,)).fetchone():
        return jsonify(ok=False, msg='已有同名师傅，请加个区分（如"张三A"）'), 400

    code = gen_engineer_code()
    service_code = gen_service_code()
    if not code or not service_code:
        return jsonify(ok=False, msg='生成失败，请重试'), 500

    pwd_hash = generate_password_hash(password)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute("""
        INSERT INTO engineers (code, name, phone, note, email, service_code, password_hash, enabled, created_at)
        VALUES (?,?,?,?,?,?,?,1,?)
    """, (code, name, phone, note, email, service_code, pwd_hash, now_str))
    db.commit()

    return jsonify(ok=True, name=name, service_code=service_code,
                   password=password, phone=phone, email=email)


@app.post('/admin/api/engineers/toggle')
@admin_required
def admin_engineers_toggle():
    d = request.get_json(silent=True) or {}
    eid = d.get('id')
    db = get_db()
    row = db.execute('SELECT enabled FROM engineers WHERE id = ?', (eid,)).fetchone()
    if not row:
        return jsonify(ok=False, msg='师傅不存在'), 404

    new_val = 0 if row['enabled'] else 1
    db.execute('UPDATE engineers SET enabled = ? WHERE id = ?', (new_val, eid))
    db.commit()
    return jsonify(ok=True, enabled=new_val)


@app.post('/admin/api/engineers/set_password')
@admin_required
def admin_engineers_set_password():
    d = request.get_json(silent=True) or {}
    eid = d.get('id')
    new_pwd = (d.get('password') or '').strip()

    if not new_pwd:
        return jsonify(ok=False, msg='请输入新密码'), 400
    if len(new_pwd) < 4:
        return jsonify(ok=False, msg='密码至少 4 位'), 400
    if len(new_pwd) > 50:
        return jsonify(ok=False, msg='密码不能超过 50 位'), 400

    db = get_db()
    eng = db.execute('SELECT * FROM engineers WHERE id = ?', (eid,)).fetchone()
    if not eng:
        return jsonify(ok=False, msg='师傅不存在'), 404

    pwd_hash = generate_password_hash(new_pwd)
    db.execute('UPDATE engineers SET password_hash = ? WHERE id = ?', (pwd_hash, eid))
    db.commit()
    return jsonify(ok=True, name=eng['name'])


@app.post('/admin/api/engineers/service_code')
@admin_required
def admin_engineers_set_service_code():
    d = request.get_json(silent=True) or {}
    eid = d.get('id')
    new_code = (d.get('service_code') or '').strip().upper()

    db = get_db()
    eng = db.execute('SELECT * FROM engineers WHERE id = ?', (eid,)).fetchone()
    if not eng:
        return jsonify(ok=False, msg='师傅不存在'), 404

    if not new_code:
        new_code = gen_service_code()
        if not new_code:
            return jsonify(ok=False, msg='生成失败，请重试'), 500
    else:
        if len(new_code) < 4 or len(new_code) > 16:
            return jsonify(ok=False, msg='服务码长度需在 4-16 位之间'), 400
        if not re.match(r'^[A-Z0-9]+$', new_code):
            return jsonify(ok=False, msg='服务码只能用大写字母和数字'), 400
        exists = db.execute(
            'SELECT 1 FROM engineers WHERE service_code = ? AND id != ?',
            (new_code, eid)
        ).fetchone()
        if exists:
            return jsonify(ok=False, msg='该服务码已被其他师傅占用'), 400

    db.execute('UPDATE engineers SET service_code = ? WHERE id = ?', (new_code, eid))
    db.commit()
    return jsonify(ok=True, service_code=new_code)


@app.post('/admin/api/engineers/set_email')
@admin_required
def admin_engineers_set_email():
    d = request.get_json(silent=True) or {}
    eid = d.get('id')
    new_email = (d.get('email') or '').strip()[:100]

    if new_email and '@' not in new_email:
        return jsonify(ok=False, msg='邮箱格式不正确'), 400

    db = get_db()
    eng = db.execute('SELECT * FROM engineers WHERE id = ?', (eid,)).fetchone()
    if not eng:
        return jsonify(ok=False, msg='师傅不存在'), 404

    db.execute('UPDATE engineers SET email = ? WHERE id = ?', (new_email, eid))
    db.commit()
    return jsonify(ok=True, name=eng['name'], email=new_email)


@app.post('/admin/api/engineers/delete')
@admin_required
def admin_engineers_delete():
    d = request.get_json(silent=True) or {}
    eid = d.get('id')
    db = get_db()

    cnt = db.execute('SELECT COUNT(*) c FROM orders WHERE engineer_id = ?',
                     (eid,)).fetchone()['c']
    if cnt > 0:
        return jsonify(ok=False,
            msg='该师傅名下还有 %d 条订单，不能删除。可改为「停用」' % cnt), 400

    cur = db.execute('DELETE FROM engineers WHERE id = ?', (eid,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify(ok=False, msg='师傅不存在'), 404
    return jsonify(ok=True)


@app.route('/admin/orders')
@admin_required
def admin_orders():
    status = request.args.get('status', '').strip()
    eng_id = request.args.get('engineer_id', '').strip()
    kw = request.args.get('kw', '').strip()

    sql = """SELECT o.*, e.name AS engineer_name
             FROM orders o LEFT JOIN engineers e ON o.engineer_id = e.id
             WHERE 1=1"""
    args = []
    if status and status in ORDER_STATUS:
        sql += ' AND o.status = ?'
        args.append(status)
    if eng_id:
        try:
            sql += ' AND o.engineer_id = ?'
            args.append(int(eng_id))
        except ValueError:
            pass
    if kw:
        sql += ' AND (o.order_no LIKE ? OR o.contact_phone LIKE ? OR o.address LIKE ?)'
        like = '%' + kw + '%'
        args += [like, like, like]
    sql += ' ORDER BY o.id DESC LIMIT 300'

    db = get_db()
    rows = db.execute(sql, args).fetchall()

    counts = {s: 0 for s in ORDER_STATUS}
    for r in db.execute('SELECT status, COUNT(*) c FROM orders GROUP BY status'):
        counts[r['status']] = r['c']

    engineers = db.execute('SELECT id, name FROM engineers WHERE enabled = 1 ORDER BY name').fetchall()

    return render_template(
        'admin_orders.html',
        orders=rows,
        status=status,
        eng_id=eng_id,
        kw=kw,
        counts=counts,
        total=sum(counts.values()),
        all_status=ORDER_STATUS,
        next_state=NEXT_STATE,
        engineers=engineers,
    )


@app.post('/admin/api/status')
@admin_required
def admin_set_status():
    d = request.get_json(silent=True) or {}
    oid = d.get('id')
    new_status = (d.get('status') or '').strip()
    if new_status not in ORDER_STATUS:
        return jsonify(ok=False, msg='非法的状态'), 400

    db = get_db()
    cur = db.execute('UPDATE orders SET status = ? WHERE id = ?', (new_status, oid))
    db.commit()
    if cur.rowcount == 0:
        return jsonify(ok=False, msg='订单不存在'), 404

    if new_status in ('已接单', '已完成'):
        row = db.execute('SELECT * FROM orders WHERE id = ?', (oid,)).fetchone()
        if row:
            try:
                notify_customer_status_change(row, new_status)
            except Exception as e:
                app.logger.warning('客户短信通知异常：%s', e)

    return jsonify(ok=True, status=new_status)


@app.post('/admin/api/assign')
@admin_required
def admin_assign_order():
    d = request.get_json(silent=True) or {}
    oid = d.get('order_id')
    eid = d.get('engineer_id')

    if not oid:
        return jsonify(ok=False, msg='缺少订单 ID'), 400

    db = get_db()
    order = db.execute('SELECT * FROM orders WHERE id = ?', (oid,)).fetchone()
    if not order:
        return jsonify(ok=False, msg='订单不存在'), 404

    # v1.0.6：已完成/已取消的订单不允许改派
    if order['status'] in ('已完成', '已取消'):
        return jsonify(ok=False,
            msg='订单已「%s」，不能改派' % order['status']), 400

    if eid in (None, '', 0, '0'):
        db.execute('UPDATE orders SET engineer_id = NULL WHERE id = ?', (oid,))
        db.commit()
        return jsonify(ok=True, msg='已取消指派', engineer_name='')

    try:
        eid_int = int(eid)
    except (TypeError, ValueError):
        return jsonify(ok=False, msg='师傅 ID 无效'), 400

    eng = db.execute('SELECT * FROM engineers WHERE id = ? AND enabled = 1',
                     (eid_int,)).fetchone()
    if not eng:
        return jsonify(ok=False, msg='师傅不存在或已停用'), 404

    if order['engineer_id'] == eid_int:
        return jsonify(ok=False, msg='该订单已归属这位师傅'), 400

    db.execute('UPDATE orders SET engineer_id = ? WHERE id = ?', (eid_int, oid))
    db.commit()

    updated = db.execute('SELECT * FROM orders WHERE id = ?', (oid,)).fetchone()
    if updated:
        try:
            _notify_engineer_assignment(updated)
        except Exception as e:
            app.logger.warning('派单通知异常：%s', e)

    return jsonify(ok=True, msg='已派给 ' + eng['name'], engineer_name=eng['name'])


# =================================================================== #
#                 v1.0.6：管理员删除订单                              #
# =================================================================== #
@app.post('/admin/api/orders/delete')
@admin_required
def admin_delete_order():
    """
    管理员删除订单。
    - 删除数据库记录
    - 同步删除照片和签名文件（如不再被其他订单引用）
    """
    d = request.get_json(silent=True) or {}
    oid = d.get('id')

    if not oid:
        return jsonify(ok=False, msg='缺少订单 ID'), 400

    db = get_db()
    order = db.execute('SELECT * FROM orders WHERE id = ?', (oid,)).fetchone()
    if not order:
        return jsonify(ok=False, msg='订单不存在'), 404

    # 收集关联文件
    files = []
    for k in ('photo1', 'photo2', 'signature'):
        if order[k]:
            files.append(order[k])

    # 删除订单记录
    db.execute('DELETE FROM orders WHERE id = ?', (oid,))
    db.commit()

    # 检查这些文件是否还被其他订单引用，没被引用就删除
    removed = 0
    for fn in files:
        still_used = db.execute("""
            SELECT 1 FROM orders
            WHERE photo1 = ? OR photo2 = ? OR signature = ?
            LIMIT 1
        """, (fn, fn, fn)).fetchone()
        if not still_used:
            try:
                os.remove(os.path.join(UPLOAD_DIR, fn))
                removed += 1
            except OSError:
                pass

    return jsonify(ok=True,
                   msg='订单已删除（清理 %d 个附件）' % removed,
                   deleted_files=removed)


@app.route('/admin/export')
@admin_required
def admin_export():
    db = get_db()
    rows = db.execute("""
        SELECT o.*, e.name AS engineer_name
        FROM orders o LEFT JOIN engineers e ON o.engineer_id = e.id
        ORDER BY o.id DESC
    """).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['订单号', '状态', '接单师傅', '服务类型', '清洗对象', '时段',
                     '预约日期', '地址', '联系电话', '备注', '照片间隔(秒)',
                     '间隔依据', '签名文件', '提交时间'])
    for r in rows:
        writer.writerow([r['order_no'], r['status'], r['engineer_name'] or '',
                         r['service_type'], r['clean_object'] or '',
                         r['time_slot'] or '', r['book_date'], r['address'],
                         r['contact_phone'], r['remark'] or '',
                         r['photo_gap'] or '',
                         r['gap_source'] or '',
                         r['signature'] or '',
                         r['created_at']])

    data = buf.getvalue().encode('utf-8-sig')
    return app.response_class(
        data, mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=orders.csv'}
    )


# =================================================================== #
#                       管理员 · 数据看板                             #
# =================================================================== #
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    return render_template('admin_dashboard.html')


@app.get('/admin/api/dashboard_data')
@admin_required
def admin_dashboard_data():
    try:
        days = int(request.args.get('days', '7'))
    except ValueError:
        days = 7
    if days not in (7, 30, 90):
        days = 7

    db = get_db()
    today = date.today()
    start = today - timedelta(days=days - 1)

    daily = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        c = db.execute(
            'SELECT COUNT(*) c FROM orders WHERE date(created_at) = ?', (d,)
        ).fetchone()['c']
        daily.append({'date': d, 'count': c})

    status_counts = {s: 0 for s in ORDER_STATUS}
    for r in db.execute('SELECT status, COUNT(*) c FROM orders GROUP BY status').fetchall():
        status_counts[r['status']] = r['c']

    engineers = []
    for r in db.execute("""
        SELECT e.id, e.name,
               COUNT(o.id) AS total,
               SUM(CASE WHEN o.status = '已完成' THEN 1 ELSE 0 END) AS done
        FROM engineers e
        LEFT JOIN orders o ON o.engineer_id = e.id
        WHERE e.enabled = 1
        GROUP BY e.id
        ORDER BY total DESC
        LIMIT 10
    """).fetchall():
        engineers.append({
            'name': r['name'],
            'total': r['total'] or 0,
            'done': r['done'] or 0,
        })

    services = []
    for r in db.execute(
        'SELECT service_type, COUNT(*) c FROM orders GROUP BY service_type'
    ).fetchall():
        services.append({'type': r['service_type'], 'count': r['c']})

    total_customers = db.execute(
        'SELECT COUNT(DISTINCT user_key) c FROM orders'
    ).fetchone()['c']
    repeat_customers = db.execute(
        'SELECT COUNT(*) c FROM ('
        '  SELECT user_key FROM orders GROUP BY user_key HAVING COUNT(*) >= 2'
        ')'
    ).fetchone()['c']
    repeat_rate = (repeat_customers / total_customers * 100) if total_customers else 0

    month_prefix = today.strftime('%Y-%m')
    month_orders = db.execute(
        "SELECT COUNT(*) c FROM orders WHERE strftime('%Y-%m', created_at) = ?",
        (month_prefix,)
    ).fetchone()['c']
    month_done = db.execute(
        "SELECT COUNT(*) c FROM orders WHERE strftime('%Y-%m', created_at) = ? AND status = '已完成'",
        (month_prefix,)
    ).fetchone()['c']

    return jsonify(
        ok=True,
        days=days,
        daily=daily,
        status_counts=status_counts,
        engineers=engineers,
        services=services,
        repeat_rate=round(repeat_rate, 1),
        repeat_customers=repeat_customers,
        total_customers=total_customers,
        total_orders=sum(status_counts.values()),
        done_orders=status_counts.get('已完成', 0),
        month_orders=month_orders,
        month_done=month_done,
    )


# =================================================================== #
#                       管理员 · 系统设置                             #
# =================================================================== #
@app.route('/admin/settings')
@admin_required
def admin_settings():
    s = get_all_settings()

    fields = {
        'email_enabled': s.get('email_enabled', '1'),
        'smtp_host': s.get('smtp_host', ''),
        'smtp_port': s.get('smtp_port', '465'),
        'smtp_user': s.get('smtp_user', ''),
        'smtp_pass': s.get('smtp_pass', ''),
        'smtp_to': s.get('smtp_to', ''),

        'sms_enabled': s.get('sms_enabled', '0'),
        'sms_provider': s.get('sms_provider', 'aliyun'),
        'sms_sign': s.get('sms_sign', ''),

        'sms_aliyun_access_key_id': s.get('sms_aliyun_access_key_id', ''),
        'sms_aliyun_access_key_secret': s.get('sms_aliyun_access_key_secret', ''),
        'sms_aliyun_template_order': s.get('sms_aliyun_template_order', ''),
        'sms_aliyun_template_accepted': s.get('sms_aliyun_template_accepted', ''),
        'sms_aliyun_template_done': s.get('sms_aliyun_template_done', ''),

        'sms_tencent_secret_id': s.get('sms_tencent_secret_id', ''),
        'sms_tencent_secret_key': s.get('sms_tencent_secret_key', ''),
        'sms_tencent_sdk_app_id': s.get('sms_tencent_sdk_app_id', ''),
        'sms_tencent_template_order': s.get('sms_tencent_template_order', ''),
        'sms_tencent_template_accepted': s.get('sms_tencent_template_accepted', ''),
        'sms_tencent_template_done': s.get('sms_tencent_template_done', ''),

        'public_url': s.get('public_url', ''),
    }

    total_size = 0
    file_count = 0
    try:
        for fn in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, fn)
            if os.path.isfile(p):
                total_size += os.path.getsize(p)
                file_count += 1
    except FileNotFoundError:
        pass

    return render_template(
        'admin_settings.html',
        s=fields,
        upload_size=total_size,
        upload_count=file_count,
        has_pil=HAS_PIL,
        has_aliyun=HAS_ALIYUN_SMS,
        has_tencent=HAS_TENCENT_SMS,
    )


@app.post('/admin/api/settings/save')
@admin_required
def admin_settings_save():
    d = request.get_json(silent=True) or {}

    allowed_keys = {
        'email_enabled', 'smtp_host', 'smtp_port', 'smtp_user', 'smtp_pass', 'smtp_to',
        'sms_enabled', 'sms_provider', 'sms_sign',
        'sms_aliyun_access_key_id', 'sms_aliyun_access_key_secret',
        'sms_aliyun_template_order', 'sms_aliyun_template_accepted', 'sms_aliyun_template_done',
        'sms_tencent_secret_id', 'sms_tencent_secret_key', 'sms_tencent_sdk_app_id',
        'sms_tencent_template_order', 'sms_tencent_template_accepted', 'sms_tencent_template_done',
        'public_url',
    }

    saved = 0
    for k, v in d.items():
        if k in allowed_keys:
            set_setting(k, str(v if v is not None else '').strip())
            saved += 1

    return jsonify(ok=True, saved=saved, msg='已保存 %d 项配置' % saved)


@app.post('/admin/api/settings/test_email')
@admin_required
def admin_settings_test_email():
    d = request.get_json(silent=True) or {}
    to = (d.get('to') or get_setting('smtp_to', '')).strip()
    if not to:
        return jsonify(ok=False, msg='请先填写收件邮箱')
    to = to.split(',')[0].strip()
    if '@' not in to:
        return jsonify(ok=False, msg='邮箱格式不正确')

    host = get_setting('smtp_host')
    port = int(get_setting('smtp_port', '465') or 465)
    user = get_setting('smtp_user')
    password = get_setting('smtp_pass')
    if not (host and user and password):
        return jsonify(ok=False, msg='请先填写并保存邮件配置')

    html = '''<h3 style="color:#2f7cf6;">✅ 测试邮件</h3>
    <p>如果你看到这封邮件，说明邮件配置正确。</p>
    <p style="color:#8b95a7;font-size:13px;">发送时间：%s</p>''' % \
        datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = formataddr(('家电清洗预约', user))
        msg['To'] = to
        msg['Subject'] = Header('【测试】家电清洗预约系统', 'utf-8')
        msg.attach(MIMEText(html, 'html', 'utf-8'))

        with smtplib.SMTP_SSL(host, port, timeout=15) as s:
            s.login(user, password)
            s.sendmail(user, [to], msg.as_string())
        return jsonify(ok=True, msg='测试邮件已发送到 %s，请查收' % to)
    except Exception as e:
        return jsonify(ok=False, msg='发送失败：%s' % e)


@app.post('/admin/api/settings/test_sms')
@admin_required
def admin_settings_test_sms():
    d = request.get_json(silent=True) or {}
    phone = (d.get('phone') or '').strip()
    if not PHONE_RE.match(phone):
        return jsonify(ok=False, msg='请输入正确的 11 位手机号')

    provider = get_setting('sms_provider', 'aliyun')
    tpl = get_setting('sms_%s_template_order' % provider)
    if not tpl:
        return jsonify(ok=False, msg='请先填写当前服务商的"新单通知"模板 ID 并保存')

    old = get_setting('sms_enabled', '0')
    set_setting('sms_enabled', '1')
    try:
        ok, msg = send_sms(phone, tpl, ['测试', '09-25 上午'])
    finally:
        set_setting('sms_enabled', old)

    if ok:
        return jsonify(ok=True, msg='测试短信已发送到 %s，请查收' % phone)
    return jsonify(ok=False, msg='发送失败：%s' % msg)


@app.post('/admin/api/cleanup')
@admin_required
def admin_cleanup():
    deleted, freed = cleanup_orphan_files()
    kb = freed / 1024
    if kb > 1024:
        human = '%.1f MB' % (kb / 1024)
    else:
        human = '%.1f KB' % kb
    return jsonify(ok=True, deleted=deleted, freed=freed,
                   msg='清理完成：删除 %d 个文件，释放 %s' % (deleted, human))


# =================================================================== #
#                         师傅端                                     #
# =================================================================== #
@app.route('/engineer/login', methods=['GET', 'POST'])
def engineer_login():
    if request.method == 'GET':
        if session.get('engineer'):
            return redirect(url_for('engineer_home'))
        return render_template('engineer_login.html')

    name = (request.form.get('name') or '').strip()
    password = (request.form.get('password') or '').strip()

    if not name or not password:
        return render_template('engineer_login.html', error='请输入姓名和密码')

    db = get_db()
    row = db.execute('SELECT * FROM engineers WHERE name = ?', (name,)).fetchone()
    if not row:
        return render_template('engineer_login.html', error='姓名或密码错误')
    if not row['password_hash'] or not check_password_hash(row['password_hash'], password):
        return render_template('engineer_login.html', error='姓名或密码错误')
    if not row['enabled']:
        return render_template('engineer_login.html', error='该账号已被停用，请联系管理员')

    session.clear()
    session['engineer'] = {
        'id': row['id'],
        'name': row['name'],
    }
    nxt = request.args.get('next')
    if nxt and nxt.startswith('/'):
        return redirect(nxt)
    return redirect(url_for('engineer_home'))


@app.post('/engineer/logout')
def engineer_logout():
    session.pop('engineer', None)
    return redirect(url_for('engineer_login'))


@app.route('/engineer')
@engineer_required
def engineer_home():
    eng = current_engineer()
    db = get_db()

    full_eng = db.execute('SELECT * FROM engineers WHERE id = ?', (eng['id'],)).fetchone()

    status = request.args.get('status', '').strip()
    sql = 'SELECT * FROM orders WHERE engineer_id = ?'
    args = [eng['id']]
    if status and status in ORDER_STATUS:
        sql += ' AND status = ?'
        args.append(status)
    sql += ' ORDER BY id DESC LIMIT 200'
    rows = db.execute(sql, args).fetchall()

    counts = {s: 0 for s in ORDER_STATUS}
    for r in db.execute(
        'SELECT status, COUNT(*) c FROM orders WHERE engineer_id = ? GROUP BY status',
        (eng['id'],)
    ):
        counts[r['status']] = r['c']

    return render_template(
        'engineer_home.html',
        engineer=full_eng,
        orders=rows,
        counts=counts,
        total=sum(counts.values()),
        all_status=ORDER_STATUS,
        next_state=NEXT_STATE,
        status=status,
    )


@app.post('/engineer/api/status')
@engineer_required
def engineer_set_status():
    d = request.get_json(silent=True) or {}
    oid = d.get('id')
    new_status = (d.get('status') or '').strip()
    if new_status not in ORDER_STATUS:
        return jsonify(ok=False, msg='非法的状态'), 400

    eng = current_engineer()
    db = get_db()

    row = db.execute('SELECT * FROM orders WHERE id = ?', (oid,)).fetchone()
    if not row:
        return jsonify(ok=False, msg='订单不存在'), 404
    if row['engineer_id'] != eng['id']:
        return jsonify(ok=False, msg='无权操作此订单'), 403

    db.execute('UPDATE orders SET status = ? WHERE id = ?', (new_status, oid))
    db.commit()

    if new_status in ('已接单', '已完成'):
        updated = db.execute('SELECT * FROM orders WHERE id = ?', (oid,)).fetchone()
        if updated:
            try:
                notify_customer_status_change(updated, new_status)
            except Exception as e:
                app.logger.warning('客户短信通知异常：%s', e)

    return jsonify(ok=True, status=new_status)


@app.post('/engineer/api/change_password')
@engineer_required
def engineer_change_password():
    d = request.get_json(silent=True) or {}
    old_pwd = (d.get('old_password') or '').strip()
    new_pwd = (d.get('new_password') or '').strip()

    if not old_pwd or not new_pwd:
        return jsonify(ok=False, msg='请填写原密码和新密码'), 400
    if len(new_pwd) < 4:
        return jsonify(ok=False, msg='新密码至少 4 位'), 400
    if len(new_pwd) > 50:
        return jsonify(ok=False, msg='新密码不能超过 50 位'), 400
    if old_pwd == new_pwd:
        return jsonify(ok=False, msg='新密码不能与原密码相同'), 400

    eng = current_engineer()
    db = get_db()
    row = db.execute('SELECT * FROM engineers WHERE id = ?', (eng['id'],)).fetchone()
    if not row:
        return jsonify(ok=False, msg='账号不存在'), 404
    if not row['password_hash'] or not check_password_hash(row['password_hash'], old_pwd):
        return jsonify(ok=False, msg='原密码错误'), 400

    new_hash = generate_password_hash(new_pwd)
    db.execute('UPDATE engineers SET password_hash = ? WHERE id = ?', (new_hash, eng['id']))
    db.commit()
    return jsonify(ok=True)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7900, debug=False)