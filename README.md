# 家电清洗服务预约系统

**版本：v1.0.3**

---

## 项目简介

一个面向家电清洗、管道疏通服务的轻量级预约管理系统。支持客户下单、师傅接单、管理员调度三种角色协作，移动端优先，Docker 一键部署。

---

## 核心功能

- **客户**：手机号登录、在线预约、手写签名、照片凭证（仅家电清洗需要）
- **师傅**：姓名 + 密码登录、专属服务码、订单状态流转、按归属收邮件
- **管理员**：师傅管理、全局订单、派单、删除订单、数据看板、系统设置
- **通用**：图片上传前自动压缩、响应式布局、SQLite WAL 模式、孤儿文件自动清理

---

## 不同账号下的功能

### 客户

- 手机号 + 服务码（**选填**）登录
- 选择服务类型、清洗对象、日期、时段
- 填写地址、电话、备注
- 阅读服务条款 + **手写签名**
- **家电清洗**：上传两张机器运行照片，间隔 ≥ 3 分钟（有 EXIF 按拍摄时间，无 EXIF 按上传时间）
- **管道疏通**：无需上传照片
- 查看自己的历史预约

### 师傅

- 用 **姓名 + 密码** 登录
- 顶部显示**专属服务码**，可复制、生成分享文案
- 只看自己名下的订单
- 一键推进：**待接单 → 已接单 → 服务中 → 已完成**
- 可取消订单、恢复待接单
- 查看客户地址、电话、照片、签名
- 修改自己的登录密码
- **按归属接收新单邮件**（管理员在"师傅管理"里给他配了邮箱时）

### 管理员

- 密码登录后台
- **师傅管理**：添加、改密码、改服务码、改邮箱、启用/停用、删除
- **全局订单**：查看所有订单、按状态/师傅筛选、改状态、**派单/改派**、**删除订单**
- **数据看板**：订单趋势、状态分布、师傅工作量、服务类型占比、复购率
- **系统设置**：邮件配置、短信配置（阿里云/腾讯云）、公网地址、孤儿文件清理
- **数据导出**：一键导出 CSV

### 自动派单规则

| 场景 | 结果 |
|---|---|
| 客户填了 A 师傅的服务码 | 订单归属 A，A 收到邮件 |
| 客户没填服务码，仅 1 个在岗师傅 | 自动分配给该师傅 |
| 客户没填服务码，多个在岗师傅 | 订单未指派，管理员手动派单 |
| 管理员手动派单 | 该师傅立即收到邮件 |

---

## 部署方法

### 目录准备

```
jiadianqingxi/
├── app.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── data/                （首次启动自动创建）
└── templates/
    ├── login.html
    ├── booking.html
    ├── orders.html
    ├── admin_login.html
    ├── admin_engineers.html
    ├── admin_orders.html
    ├── admin_dashboard.html
    ├── admin_settings.html
    ├── engineer_login.html
    └── engineer_home.html
```

### `requirements.txt`

```
Flask==3.0.3
Pillow==10.4.0
alibabacloud_dysmsapi20170525==2.0.24
alibabacloud_tea_openapi==0.3.10
alibabacloud_tea_util==0.3.12
tencentcloud-sdk-python==3.0.1180
```

### `Dockerfile`

```dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py .
COPY templates/ templates/

RUN mkdir -p /app/data/uploads

EXPOSE 7900

CMD ["python", "app.py"]
```

### `docker-compose.yml`

```yaml
version: '3.8'

services:
  jiadian:
    build: .
    image: jiadianqingxi:1.0.3
    container_name: jiadianqingxi
    ports:
      - "7900:7900"
    environment:
      # ===== 必填：改成你自己的 =====
      SECRET_KEY: "改成你自己的随机字符串，建议 40 位以上"
      ADMIN_PASSWORD: "admin123"
      # ===== 可选 =====
      PHOTO_MIN_INTERVAL: "180"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

### 启动

```bash
cd /vol1/1000/uploads/jiadianqingxi
sudo docker compose up -d --build
```

### 更新代码后重启

```bash
sudo docker compose up -d --build
```

**环境变量、数据、卷全都不变**，只有代码更新。

---

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `SECRET_KEY` | ✅ | `dev-secret-key-change-me` | 会话签名密钥，改后所有用户需重新登录 |
| `ADMIN_PASSWORD` | ✅ | `admin123` | 管理员后台密码 |
| `PHOTO_MIN_INTERVAL` | ❌ | `180` | 两张照片最小间隔秒数，调试可改 `5` |

**邮件和短信配置在管理员后台配置，不需要环境变量。**

生成 `SECRET_KEY`：

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## 三个登录入口

| 角色 | 地址 | 用什么登录 |
|---|---|---|
| 客户 | `/login` | 手机号 + 服务码（选填） |
| 师傅 | `/engineer/login` | 姓名 + 密码 |
| 管理员 | `/admin/login` | 管理员密码 |

`/login` 页面右上角有"师傅登录 / 管理员登录"快捷入口。

---

## 邮件配置（管理员后台）

### 获取 QQ 邮箱授权码

1. 网页登录 https://mail.qq.com
2. **设置** → **账户**
3. 找到 **POP3/IMAP/SMTP** 服务，开启 **IMAP/SMTP**
4. 手机发短信验证
5. 弹出的 **16 位字符串** 就是授权码（不是 QQ 密码）

### 在系统后台填入

1. 登录 `/admin/login` → **系统设置** → 展开 **邮件通知** 卡片
2. 填写：
   - SMTP 服务器：`smtp.qq.com`
   - 端口：`465`
   - 发件邮箱：你的 QQ 邮箱
   - 授权码：16 位授权码
   - **管理员收件邮箱**：必填，多个用英文逗号分隔
3. 点 **发送测试邮件** 验证
4. 打开"启用邮件通知"开关

### 给师傅配邮箱

**师傅邮箱不填在"管理员收件邮箱"里**，去：

**师傅管理 → 找对应师傅 → 点"改邮箱"** → 填他的邮箱 → 保存

系统会按订单归属自动发：

| 场景 | 收件人 |
|---|---|
| 客户填了 A 的服务码 | 管理员 + A 师傅 |
| 客户没填服务码 | 仅管理员 |
| 管理员派单给 A | A 师傅 |

**建议**：让师傅把邮箱绑定微信（微信搜"QQ邮箱提醒"公众号），新单会直接弹微信通知。

---

## 配置短信（可选）

### 前提

个人实名认证的阿里云账号**无法申请"通知"类模板**，需要企业资质。

### 步骤

1. 去阿里云或腾讯云后台，申请**"通知短信"类型**的模板：

| 用途 | 模板内容 |
|---|---|
| 新单通知（给师傅） | `您有新预约单，客户{1}，预约时间{2}，请登录工作台查看详情。` |
| 接单通知（给客户） | `您的家电清洗预约已被{1}接单，师傅将提前电话与您联系。` |
| 完成通知（给客户） | `您的家电清洗服务已完成，感谢使用。` |

2. 审核通过后，在系统后台"系统设置 → 短信通知"里：
   - 选择服务商
   - 填 AccessKey、模板 ID、短信签名
   - 点"发送测试短信"验证
   - 打开"启用短信通知"开关

### 短信触发点

| 事件 | 发给谁 |
|---|---|
| 客户下单 | 归属师傅 |
| 师傅接单 | 客户 |
| 师傅完成 | 客户 |
| 管理员改状态 | 客户 |

---

## 使用方法

### 管理员首次部署

1. 打开 `/admin/login`，用 `ADMIN_PASSWORD` 登录
2. **系统设置** → 配置邮件
3. **师傅管理** → 添加师傅（填姓名、手机号、邮箱、初始密码）
4. 弹窗显示"登录姓名 + 密码 + 服务码"，点"复制全部信息"发给师傅

### 师傅首次使用

1. 打开 `/engineer/login`，用"姓名 + 密码"登录
2. 工作台顶部显示自己的**服务码**
3. 点 **"生成分享文案"**，复制后通过微信发给客户
4. 客户下单后，工作台出现新订单
5. 依次点 **接单 →** **开始服务 →** **完成服务 →** 推进

### 客户下单

1. 点师傅发来的链接（链接里带服务码，自动填入）
2. 填手机号，点"登录并预约"
3. 填写预约信息、勾选条款、手写签名
4. **家电清洗**：上传两张照片，间隔 3 分钟以上
5. **管道疏通**：无需上传照片
6. 提交

---

## 订单状态

```
待接单 ──[接单]──> 已接单 ──[开始服务]──> 服务中 ──[完成服务]──> 已完成
  │                  │                    │
  └──[取消订单]──────┴────[取消订单]──────┘
                                            │
                                            ▼
                                         已取消
                                            │
                                     [恢复待接单]
```

**已完成/已取消的订单不能改派**（避免师傅收到已结束的单）。

---

## 系统维护

### 孤儿文件清理

- **自动**：每 24 小时后台跑一次
- **手动**：管理员后台 → 系统设置 → 维护 → 点"清理孤儿文件"

**什么是孤儿文件**：用户上传了照片或签名但没提交订单，文件留在 `uploads/` 里但数据库没引用。

### 删除订单

管理员在"全局订单"里，每行有"删除"按钮：

- 删除订单记录
- 同步清理照片和签名（如不再被其他订单引用）
- 需要**二次确认**（输入订单号后 4 位）

### 数据备份

```bash
tar czf backup-$(date +%F).tar.gz /vol1/1000/uploads/jiadianqingxi/data/
```

### 常用命令

```bash
# 查看日志
sudo docker logs -f jiadianqingxi

# 重启
sudo docker compose restart

# 停止并删除容器（数据不丢）
sudo docker compose down

# 更新代码
sudo docker compose up -d --build

# 备份
tar czf backup-$(date +%F).tar.gz /vol1/1000/uploads/jiadianqingxi/data/
```

---

## 代码结构

| 文件 | 说明 |
|---|---|
| `app.py` | 后端全部逻辑（Flask + SQLite） |
| `templates/login.html` | 客户登录 |
| `templates/booking.html` | 客户预约下单 |
| `templates/orders.html` | 客户查看预约 |
| `templates/admin_login.html` | 管理员登录 |
| `templates/admin_engineers.html` | 师傅管理 |
| `templates/admin_orders.html` | 全局订单 |
| `templates/admin_dashboard.html` | 数据看板 |
| `templates/admin_settings.html` | 系统设置 |
| `templates/engineer_login.html` | 师傅登录 |
| `templates/engineer_home.html` | 师傅工作台 |
| `data/data.db` | SQLite 数据库 |
| `data/uploads/` | 照片和签名文件 |

---

## 已知限制

- SQLite 单文件数据库，适合中小规模
- 只有一个管理员密码，暂不支持多管理员分权
- 短信通知需要企业资质（个人实名无法申请"通知"类模板）
- 邮件图片链接依赖 `PUBLIC_URL` 配置正确，否则外网收件人看不到照片
- 短信模板内容需在服务商后台申请审核，无法在系统里直接编辑

---

## 开发者信息

- 开发者：晦华先生
- 邮箱：2303537063@qq.com

---

## License

MIT