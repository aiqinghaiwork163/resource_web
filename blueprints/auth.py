"""auth blueprint (split from app.py)."""


from flask import Blueprint


import re


import secrets


from flask import Flask, jsonify, request, send_from_directory, session, redirect


from requests_oauthlib import OAuth2Session


from .common import GITHUB_API_URL, GITHUB_AUTHORIZATION_BASE_URL, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_TOKEN_URL, TG_LOGIN_TTL, _bind_telegram_to_user, _build_oauth_redirect_uri, _ensure_telegram_id_column, _ensure_telegram_settings_keys, _ensure_telegram_webhook, _find_or_create_oauth_user, _find_or_create_telegram_user, _grant_session, _growth_info, _is_foreign_ip, _is_staff_role, _is_superadmin_role, _norm_role, _oauth_http_proxies, _pack_oauth_state, _promote_telegram_superadmin, _record_group_chat_activity, _safe_next_url, _session_login_user, _setting_db, _telegram_api, _telegram_bot_token, _telegram_bot_username, _telegram_group_gate, _telegram_required_chat, _telegram_webhook_secret, _tg_bind_pop, _tg_handle_inline_query, _tg_handle_search, _tg_help_text, _tg_login_consume, _tg_login_get, _tg_login_mark_ready, _tg_login_store_pending, _unpack_oauth_state, _user_pass_internal_beta_gate, _verify_telegram_login, app, check_login_limit, csrf, get_client_ip, get_db, get_redis, hash_password, record_login_failure, reset_login_failures, verify_password


auth_bp = Blueprint('auth', __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        data = request.get_json() or request.form
        username = data.get("username", "").strip()
        password = data.get("password", "")
        client_ip = get_client_ip()
        # 检查登录限流
        allowed, remaining = check_login_limit(client_ip)
        if not allowed:
            return jsonify({"ok": False, "error": f"登录尝试过多，请{remaining}秒后再试"}), 429
        if not username or not password:
            return jsonify({"ok": False, "error": "请输入用户名和密码"}), 401
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("SELECT id, username, role, status, password, telegram_id FROM users WHERE username=%s OR email=%s LIMIT 1", (username, username.lower()))
            user = cur.fetchone()
        finally:
            db.close()
        if not user:
            record_login_failure(client_ip)
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        if user["status"] != 1:
            return jsonify({"ok": False, "error": "账号已被禁用"}), 401
        # 验证密码（兼容旧版 SHA256，验证成功后自动迁移到 bcrypt）
        stored_pw = user.get("password", "")
        if not verify_password(password, stored_pw):
            record_login_failure(client_ip)
            return jsonify({"ok": False, "error": "用户名或密码错误"}), 401
        # 密码验证成功。若仍是旧 SHA256 格式，此时才迁移到 bcrypt。
        # 注意: 迁移必须放在验证成功分支。此前误放在 `if not verify_password(...)`
        # 分支内，导致密码【错误】时用攻击者输入的密码覆写数据库，
        # 攻击者两次请求即可接管任意 SHA256 账号。
        if stored_pw and not stored_pw.startswith('$2b$') and not stored_pw.startswith('$2a$'):
            new_hash = hash_password(password)
            db2 = get_db()
            try:
                db2.cursor().execute("UPDATE users SET password=%s WHERE id=%s", (new_hash, user["id"]))
                db2.commit()
            except Exception as e:
                app.logger.warning("bcrypt 迁移失败 user_id=%s: %s", user["id"], e)
            finally:
                db2.close()
        # TG 超管名单对上则升为 superadmin（密码登录也会生效）
        if user.get("telegram_id"):
            db3 = get_db()
            try:
                cur3 = db3.cursor()
                user = _promote_telegram_superadmin(cur3, user, user.get("telegram_id"))
                db3.commit()
            finally:
                db3.close()
        # 纯内测：不在必加群一律不能登录（含管理员密码；超管 TG 可兜底）
        if _telegram_required_chat():
            ok_gate, gate_msg = _user_pass_internal_beta_gate(user_id=user["id"], role=user.get("role"))
            if not ok_gate:
                return jsonify({"ok": False, "error": gate_msg}), 403
        session["is_admin"] = _is_staff_role(user.get("role"))
        session["is_superadmin"] = _is_superadmin_role(user.get("role"))
        session["role"] = _norm_role(user.get("role"))
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        # 重新生成 session ID，防止会话固定攻击
        session.modified = True
        reset_login_failures(client_ip)
        # 更新最后登录时间
        db2 = get_db()
        try:
            db2.cursor().execute("UPDATE users SET last_login=NOW() WHERE id=%s", (user["id"],))
            db2.commit()
        finally:
            db2.close()
        if request.is_json:
            next_url = _safe_next_url(
                (data.get("next") if isinstance(data, dict) else None)
                or request.args.get("next")
                or "/"
            )
            if _is_staff_role(user.get("role")) and next_url in ("/", ""):
                next_url = "/admin"
            return jsonify({"ok": True, "role": _norm_role(user.get("role")), "redirect": next_url})
        next_url = _safe_next_url(request.args.get("next") or "/")
        if _is_staff_role(user.get("role")) and next_url in ("/", ""):
            return redirect("/admin")
        return redirect(next_url)
    resp = send_from_directory(".", "login.html")
    resp.cache_control.max_age = 0
    resp.cache_control.no_cache = True
    resp.cache_control.no_store = True
    resp.cache_control.must_revalidate = True
    return resp


@auth_bp.route("/api/telegram/login-config")
def api_telegram_login_config():
    """前端拉 Bot 用户名及第三方登录开关与 IP 限制"""
    _ensure_telegram_settings_keys()
    token = _telegram_bot_token()
    bot_username = _telegram_bot_username()
    
    tg_switch = _setting_db("enable_telegram_login")
    if tg_switch is None:
        tg_switch = "1"
    tg_enabled = bool(token and bot_username and str(tg_switch).strip() in ("1", "true", "on"))

    gh_switch = _setting_db("enable_github_login")
    if gh_switch is None:
        gh_switch = "1"
    gh_enabled = bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET and str(gh_switch).strip() in ("1", "true", "on"))

    client_ip = get_client_ip()
    is_foreign = _is_foreign_ip(client_ip)

    chat = _telegram_required_chat()
    chat_link = ""
    if chat:
        chat_link = chat if chat.startswith("http") else f"https://t.me/{chat.lstrip('@')}"
    return jsonify({
        "enabled": tg_enabled,
        "github_enabled": gh_enabled,
        "is_foreign": is_foreign,
        "bot_username": bot_username or "vaultdrive_bot",
        "login_bot": bot_username or "vaultdrive_bot",
        "required_chat": chat,
        "required_chat_link": chat_link,
        "growth": _growth_info(),
        "hint": "" if tg_enabled else "Telegram 登录已关闭或未配置",
    })


@auth_bp.route("/login/telegram", methods=["POST"])
def login_telegram():
    """Telegram Login Widget → JSON POST 登录"""
    _ensure_telegram_id_column()
    _ensure_telegram_settings_keys()
    if not _telegram_bot_token():
        return jsonify({"ok": False, "error": "服务端未配置 Telegram 登录"}), 500

    data = request.get_json(silent=True) or {}
    payload = {
        "id": data.get("id"),
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "username": data.get("username"),
        "photo_url": data.get("photo_url"),
        "auth_date": data.get("auth_date"),
        "hash": data.get("hash"),
    }
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}

    ok, err = _verify_telegram_login(payload)
    if not ok:
        return jsonify({"ok": False, "error": err}), 401

    tg_id = payload.get("id")
    if not tg_id:
        return jsonify({"ok": False, "error": "缺少 Telegram id"}), 400

    ok_gate, gate_msg = _telegram_group_gate(tg_id)
    if not ok_gate:
        return jsonify({"ok": False, "error": gate_msg}), 403

    current_uid = session.get("user_id")
    if current_uid:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT id, username, role, status, telegram_id FROM users WHERE id=%s",
                (current_uid,),
            )
            current_user = cur.fetchone()
            if current_user and not current_user.get("telegram_id"):
                cur.execute(
                    "SELECT id FROM users WHERE telegram_id=%s AND id!=%s",
                    (str(tg_id), current_uid),
                )
                if cur.fetchone():
                    return jsonify({"ok": False, "error": "该 Telegram 已绑定其他账号"}), 409
                cur.execute(
                    "UPDATE users SET telegram_id=%s WHERE id=%s",
                    (str(tg_id), current_uid),
                )
                current_user = _promote_telegram_superadmin(cur, current_user, tg_id)
                db.commit()
                _session_login_user(current_user)
                return jsonify({"ok": True, "role": current_user["role"], "bound": True})
        finally:
            db.close()

    user, error = _find_or_create_telegram_user(
        tg_id,
        payload.get("username"),
        payload.get("first_name"),
        payload.get("last_name"),
    )
    if error:
        return jsonify({"ok": False, "error": error}), 403 if "禁用" in error else 400
    if not user:
        return jsonify({"ok": False, "error": "登录失败"}), 500

    _session_login_user(user)
    return jsonify({"ok": True, "role": user["role"], "username": user["username"]})


@auth_bp.route("/login/telegram/callback")
@csrf.exempt
def login_telegram_callback():
    """data-auth-url 回调（query 参数）；校验后写 session 并跳转"""
    _ensure_telegram_id_column()
    payload = {k: request.args.get(k) for k in (
        "id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash"
    ) if request.args.get(k) is not None}
    ok, err = _verify_telegram_login(payload)
    if not ok:
        return redirect(f"/login?error=tg_{err}")
    user, error = _find_or_create_telegram_user(
        payload.get("id"),
        payload.get("username"),
        payload.get("first_name"),
        payload.get("last_name"),
    )
    if error or not user:
        return redirect(f"/login?error=tg_{error or 'fail'}")
    _session_login_user(user)
    return redirect("/admin" if _is_staff_role(user.get("role")) else "/")


@auth_bp.route("/api/auth/telegram/login-code", methods=["POST"])
def api_telegram_login_code():
    """未登录：生成 Bot 深链登录码（打开 Bot 确认后网页轮询完成登录）"""
    if session.get("user_id"):
        return jsonify({"ok": True, "already": True, "redirect": "/"}), 200
    if not _telegram_bot_token() or not _telegram_bot_username():
        return jsonify({"error": "服务端未配置 Telegram Bot"}), 500
    ok, info = _ensure_telegram_webhook()
    if not ok:
        return jsonify({"error": f"Webhook 未就绪: {info}"}), 503
    code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    try:
        _tg_login_store_pending(code)
    except Exception as e:
        app.logger.exception("api_telegram_login_code failed")
        return jsonify({"error": str(e)}), 500
    bot = _telegram_bot_username()
    return jsonify({
        "ok": True,
        "code": code,
        "deep_link": f"https://t.me/{bot}?start=login_{code}",
        "bot_username": bot,
        "expires_in": TG_LOGIN_TTL,
    })


@auth_bp.route("/api/auth/telegram/login-poll")
def api_telegram_login_poll():
    code = re.sub(r"[^A-Za-z0-9]", "", (request.args.get("code") or ""))
    if not code:
        return jsonify({"status": "invalid"}), 400
    data = _tg_login_get(code)
    if not data:
        return jsonify({"status": "expired"})
    return jsonify({"status": data.get("status") or "pending"})


@auth_bp.route("/api/auth/telegram/login-complete", methods=["POST"])
def api_telegram_login_complete():
    """轮询到 ready 后调用，写入 session"""
    body = request.get_json(silent=True) or {}
    code = re.sub(r"[^A-Za-z0-9]", "", str(body.get("code") or ""))
    if not code:
        return jsonify({"ok": False, "error": "缺少 code"}), 400
    data = _tg_login_consume(code)
    if not data or data.get("status") != "ready" or not data.get("tg_id"):
        return jsonify({"ok": False, "error": "登录码无效或未确认"}), 400
    user, error = _find_or_create_telegram_user(
        data.get("tg_id"),
        data.get("username"),
        data.get("first_name"),
        data.get("last_name"),
    )
    if error or not user:
        return jsonify({"ok": False, "error": error or "登录失败"}), 403 if error and "禁用" in str(error) else 400
    _session_login_user(user)
    return jsonify({"ok": True, "role": user["role"], "username": user["username"]})


@auth_bp.route("/api/telegram/bot-webhook/<secret>", methods=["POST"])
@csrf.exempt
def api_telegram_bot_webhook(secret):
    """Bot 更新：登录/绑定 + 资源搜索（私聊片名、/s、inline）"""
    if not hmac.compare_digest(secret, _telegram_webhook_secret()):
        return jsonify({"ok": False}), 403
    update = request.get_json(silent=True) or {}

    iq = update.get("inline_query")
    if iq:
        try:
            _tg_handle_inline_query(iq)
        except Exception as e:
            app.logger.warning("inline_query failed: %s", e)
        return jsonify({"ok": True})

    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_user = msg.get("from") or {}
    text = (msg.get("text") or "").strip()
    try:
        _record_group_chat_activity(chat, from_user)
    except Exception:
        app.logger.exception("api_telegram_bot_webhook failed")
        pass
    if not chat_id or not text:
        return jsonify({"ok": True})

    action = None  # bind | login
    code = None
    lower = text.lower()
    if lower.startswith("/start"):
        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""
        pl = payload.lower()
        if pl.startswith("bind_"):
            action, code = "bind", payload[5:].strip()
        elif pl.startswith("bind"):
            action, code = "bind", payload[4:].lstrip("_").strip()
        elif pl.startswith("login_"):
            action, code = "login", payload[6:].strip()
        elif pl.startswith("login"):
            action, code = "login", payload[5:].lstrip("_").strip()
    elif lower.startswith("/bind"):
        parts = text.split(maxsplit=1)
        action, code = "bind", (parts[1].strip() if len(parts) > 1 else "")
    elif lower.startswith("/login"):
        parts = text.split(maxsplit=1)
        action, code = "login", (parts[1].strip() if len(parts) > 1 else "")

    def _reply(body):
        try:
            _telegram_api("sendMessage", {
                "chat_id": chat_id,
                "text": body,
                "disable_web_page_preview": True,
            })
        except Exception as e:
            app.logger.warning("TG sendMessage failed: %s", e)

    if action and code:
        code = re.sub(r"[^A-Za-z0-9]", "", code)
        if not code:
            _reply("码无效，请回网站重新操作。")
            return jsonify({"ok": True})

        tg_id = from_user.get("id")
        if not tg_id:
            _reply("无法读取你的 Telegram ID。")
            return jsonify({"ok": True})

        if action == "login":
            ok_gate, gate_msg = _telegram_group_gate(tg_id)
            if not ok_gate:
                _reply(gate_msg)
                return jsonify({"ok": True})
            if not _tg_login_mark_ready(code, from_user):
                _reply("登录码无效或已过期，请回网站重新点击登录。")
                return jsonify({"ok": True})
            uname = from_user.get("username") or from_user.get("first_name") or ""
            _reply(
                "登录确认成功 ✅\n"
                f"Telegram ID: {tg_id}"
                + (f"\n@{uname}" if from_user.get("username") else "")
                + "\n\n请回到网页，会自动完成登录。"
            )
            return jsonify({"ok": True})

        uid = _tg_bind_pop(code)
        if not uid:
            _reply("绑定码无效或已过期，请回网站重新生成后重试。")
            return jsonify({"ok": True})

        ok, status = _bind_telegram_to_user(uid, tg_id)
        if not ok:
            _reply(f"绑定失败：{status}")
            return jsonify({"ok": True})

        uname = from_user.get("username") or from_user.get("first_name") or ""
        if status == "already":
            _reply(f"已经绑定过了 ✅\nTelegram ID: {tg_id}" + (f"\n@{uname}" if uname else ""))
        else:
            _reply(
                f"绑定成功 ✅\n"
                f"Telegram ID: {tg_id}"
                + (f"\n@{uname}" if from_user.get("username") else "")
                + "\n\n可回网站「我的收藏」查看。"
            )
        return jsonify({"ok": True})

    if lower.startswith("/start") or lower.startswith("/help") or lower in ("help", "/help@vaultdrive_bot"):
        _reply(_tg_help_text())
        return jsonify({"ok": True})

    search_kw = None
    m = re.match(r"^/(s|search|so)(?:@\w+)?(?:\s+(.*))?$", text, re.I)
    if m:
        search_kw = (m.group(2) or "").strip()
    elif (chat.get("type") or "") == "private" and not text.startswith("/"):
        search_kw = text

    if search_kw is not None:
        _tg_handle_search(from_user, _reply, search_kw)
        return jsonify({"ok": True})

    return jsonify({"ok": True})


@auth_bp.route("/login/github")
def github_login():
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return jsonify({"error": "服务端未配置 GitHub OAuth"}), 500
    # next=/profile 等站内路径；bind=1 表示给当前登录用户绑定，不要切号
    next_url = (request.args.get("next") or "").strip()
    if not (next_url.startswith("/") and not next_url.startswith("//")):
        next_url = "/"
    oauth_bind = request.args.get("bind") in ("1", "true", "yes")
    session["oauth_next"] = next_url
    if oauth_bind:
        session["oauth_bind"] = True
    # state 自带 bind/uid/next，公网回调即使丢 cookie 也能绑定
    state = _pack_oauth_state(
        next_url=next_url,
        bind=oauth_bind,
        uid=session.get("user_id"),
    )
    session["oauth_state"] = state
    redirect_uri = _build_oauth_redirect_uri()
    github = OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=["user:email"],
    )
    authorization_url, _ = github.authorization_url(
        GITHUB_AUTHORIZATION_BASE_URL,
        state=state,
    )
    return redirect(authorization_url)


@auth_bp.route("/login/github/callback")
def github_callback():
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return redirect("/login")
    raw_state = request.args.get("state")
    meta = _unpack_oauth_state(raw_state) or {}
    # 优先用签名 state；同域时再回退 flask session
    oauth_next = meta.get("next") or session.pop("oauth_next", None) or "/"
    oauth_bind = bool(meta.get("bind") if "bind" in meta else session.pop("oauth_bind", None))
    session.pop("oauth_state", None)
    if not (isinstance(oauth_next, str) and oauth_next.startswith("/") and not oauth_next.startswith("//")):
        oauth_next = "/"
    redirect_uri = _build_oauth_redirect_uri()
    github = OAuth2Session(
        GITHUB_CLIENT_ID,
        redirect_uri=redirect_uri,
        scope=["user:email"],
    )
    proxies = _oauth_http_proxies()
    try:
        github.fetch_token(
            GITHUB_TOKEN_URL,
            client_id=GITHUB_CLIENT_ID,
            client_secret=GITHUB_CLIENT_SECRET,
            code=request.args.get("code"),
            timeout=20,
            proxies=proxies,
        )
    except Exception as e:
        app.logger.warning("GitHub OAuth token 获取失败: %s", e)
        dest = "/profile" if oauth_bind else "/login"
        return redirect(f"{dest}?error=oauth_failed")
    try:
        user_resp = github.get(GITHUB_API_URL, timeout=20, proxies=proxies)
        user_data = user_resp.json()
    except Exception as e:
        app.logger.warning("GitHub API 请求失败: %s", e)
        dest = "/profile" if oauth_bind else "/login"
        return redirect(f"{dest}?error=oauth_api_failed")

    github_id = user_data.get("id")
    username = user_data.get("login")
    email = user_data.get("email")
    if not github_id:
        return redirect("/login?error=oauth_no_id")

    # 如果当前已登录（或 state 里带了 uid），尝试绑定到该账号
    current_uid = session.get("user_id") or meta.get("uid")
    if current_uid:
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT id, username, role, status, github_id FROM users WHERE id=%s",
                (current_uid,),
            )
            current_user = cur.fetchone()
            if not current_user:
                pass
            elif current_user.get("github_id"):
                # 已绑定：回到 next，不切号
                return redirect(oauth_next if oauth_bind or oauth_next != "/" else (
                    "/admin" if _is_staff_role(current_user.get("role")) else "/"
                ))
            else:
                cur.execute(
                    "SELECT id FROM users WHERE github_id=%s AND id<>%s",
                    (str(github_id), current_uid),
                )
                if cur.fetchone():
                    dest = "/profile" if (oauth_bind or oauth_next.startswith("/profile")) else oauth_next
                    sep = "&" if "?" in dest else "?"
                    return redirect(f"{dest}{sep}error=github_taken")
                cur.execute(
                    "UPDATE users SET github_id=%s WHERE id=%s",
                    (str(github_id), current_uid),
                )
                db.commit()
                app.logger.info("用户 %s 绑定 GitHub ID %s (@%s)", current_uid, github_id, username)
                _grant_session(current_user, touch_login=False)
                dest = oauth_next if oauth_bind or oauth_next != "/" else (
                    "/admin" if _is_staff_role(current_user.get("role")) else "/"
                )
                if dest.startswith("/profile"):
                    sep = "&" if "?" in dest else "?"
                    return redirect(f"{dest}{sep}bound=github")
                return redirect(dest)
        except Exception as e:
            app.logger.warning("GitHub 绑定失败: %s", e)
            if oauth_bind:
                return redirect("/profile?error=bind_failed")
        finally:
            db.close()
        if oauth_bind:
            return redirect("/profile?error=bind_failed")

    user, error = _find_or_create_oauth_user(github_id, username, email)
    if error:
        return redirect(f"/login?error={error}")

    _grant_session(user, touch_login=True)
    if oauth_next and oauth_next not in ("/", "/login"):
        return redirect(oauth_next)
    return redirect("/admin" if _is_staff_role(user.get("role")) else "/")


@auth_bp.route("/logout")
def logout():
    session.clear()
    next_url = (request.args.get("next") or "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect("/login")


@auth_bp.route("/api/auth/email/send-code", methods=["POST"])
@csrf.exempt
def api_email_send_code():
    """发送 6 位邮箱验证码（防刷：60秒CD，10分钟有效）"""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    action = str(data.get("action") or "register").strip().lower()

    if not email or not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        return jsonify({"ok": False, "error": "请输入有效的电子邮箱地址"}), 400

    r = get_redis()
    cd_key = f"res_web:email_cd:{email}:{action}"
    if r and r.get(cd_key):
        ttl = r.ttl(cd_key)
        return jsonify({"ok": False, "error": f"发送太频繁，请在 {ttl} 秒后再试"}), 429

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id FROM users WHERE email=%s LIMIT 1", (email,))
        existing_user = cur.fetchone()
        if action == "register" and existing_user:
            return jsonify({"ok": False, "error": "该邮箱已被注册，请直接登录"}), 400
        elif action in ("login", "reset") and not existing_user:
            return jsonify({"ok": False, "error": "该邮箱尚未注册，请先注册"}), 400
        elif action == "bind":
            my_uid = session.get("user_id")
            if not my_uid:
                return jsonify({"ok": False, "error": "请先登录后再绑定邮箱"}), 401
            if existing_user and existing_user["id"] != my_uid:
                return jsonify({"ok": False, "error": "该邮箱已被其他账号绑定"}), 400
    finally:
        db.close()

    import random
    code = f"{random.randint(100000, 999999)}"

    # 发送邮件
    try:
        from services import mailer
        ok, msg = mailer.send_otp_email(to_email=email, code=code, action=action)
        if not ok:
            return jsonify({"ok": False, "error": f"邮件发送失败: {msg}"}), 500
    except Exception as e:
        app.logger.exception("api_email_send_code failed")
        return jsonify({"ok": False, "error": f"发信服务异常: {str(e)}"}), 500

    # 写入 Redis 缓存验证码 (10分钟有效，60秒限制)
    if r:
        r.setex(f"res_web:email_otp:{email}:{action}", 600, code)
        r.setex(cd_key, 60, "1")

    return jsonify({"ok": True, "message": "验证码已发送至您的邮箱，10分钟内有效"})


@auth_bp.route("/api/auth/email/reset-password", methods=["POST"])
@csrf.exempt
def api_email_reset_password():
    """邮箱验证码重置密码（忘记密码）"""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()
    new_password = str(data.get("password") or "").strip()

    if not email or not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        return jsonify({"ok": False, "error": "请输入有效的电子邮箱地址"}), 400
    if not code or len(code) != 6:
        return jsonify({"ok": False, "error": "请输入6位有效数字验证码"}), 400
    if not new_password or len(new_password) < 6:
        return jsonify({"ok": False, "error": "新密码长度至少需要 6 个字符"}), 400

    r = get_redis()
    otp_key = f"res_web:email_otp:{email}:reset"
    cached_code = r.get(otp_key).decode("utf-8") if (r and r.get(otp_key)) else None

    if not cached_code or cached_code != code:
        return jsonify({"ok": False, "error": "验证码错误或已过期，请重新获取"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT id, username FROM users WHERE email=%s LIMIT 1", (email,))
        user = cur.fetchone()
        if not user:
            return jsonify({"ok": False, "error": "该邮箱未绑定任何账号"}), 404

        pw_hash = hash_password(new_password)
        cur.execute("UPDATE users SET password=%s, updated_at=NOW() WHERE id=%s", (pw_hash, user["id"]))
        db.commit()

        if r:
            r.delete(otp_key)

        return jsonify({"ok": True, "message": "密码重置成功！请使用新密码登录"})
    finally:
        db.close()


@auth_bp.route("/api/auth/email/register", methods=["POST"])
@csrf.exempt
def api_email_register():
    """邮箱 + 验证码注册新账号"""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()
    password = str(data.get("password") or "").strip()
    username = str(data.get("username") or "").strip()

    if not email or not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        return jsonify({"ok": False, "error": "请输入有效的电子邮箱地址"}), 400
    if not code or len(code) != 6:
        return jsonify({"ok": False, "error": "请输入6位有效数字验证码"}), 400
    if not username or len(username) < 3 or len(username) > 30:
        return jsonify({"ok": False, "error": "用户名长度需在 3-30 个字符之间"}), 400
    if not password or len(password) < 6:
        return jsonify({"ok": False, "error": "密码长度至少需要 6 个字符"}), 400

    r = get_redis()
    otp_key = f"res_web:email_otp:{email}:register"
    cached_code = r.get(otp_key).decode("utf-8") if (r and r.get(otp_key)) else None

    # 若未命中缓存或不一致
    if not cached_code or cached_code != code:
        return jsonify({"ok": False, "error": "验证码错误或已过期，请重新获取"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        # 检查邮箱和用户名
        cur.execute("SELECT id FROM users WHERE email=%s LIMIT 1", (email,))
        if cur.fetchone():
            return jsonify({"ok": False, "error": "该邮箱已被注册，请直接登录"}), 400

        cur.execute("SELECT id FROM users WHERE username=%s LIMIT 1", (username,))
        if cur.fetchone():
            return jsonify({"ok": False, "error": "该用户名已被占用，请换一个"}), 400

        pw_hash = hash_password(password)
        cur.execute(
            """
            INSERT INTO users (username, email, password, role, status, points, created_at, updated_at)
            VALUES (%s, %s, %s, 'user', 1, 100, NOW(), NOW())
            """,
            (username, email, pw_hash),
        )
        new_uid = cur.lastrowid
        cur.execute(
            """
            INSERT INTO point_logs (user_id, action, change_amount, balance_after, remark, created_at)
            VALUES (%s, 'welcome', 100, 100, '新用户邮箱注册奖励', NOW())
            """,
            (new_uid,),
        )
        db.commit()

        # 消费掉验证码
        if r:
            r.delete(otp_key)

        # 建立 session 自动登录
        session["user_id"] = new_uid
        session["username"] = username
        session["role"] = "user"
        session["is_admin"] = False
        session["is_superadmin"] = False
        session.permanent = True
        session.modified = True

        next_url = _safe_next_url(data.get("next") or request.args.get("next") or "/profile")
        return jsonify({
            "ok": True,
            "message": "注册成功并已自动登录",
            "user_id": new_uid,
            "username": username,
            "role": "user",
            "redirect": next_url,
        })
    finally:
        db.close()


@auth_bp.route("/register", methods=["POST"])
@csrf.exempt
def api_register():
    """新用户注册（支持邀请码或普通注册）"""
    data = request.get_json() or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    invite_code = str(data.get("invite_code") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "请输入用户名和密码"}), 400
    if len(username) < 3 or len(username) > 30:
        return jsonify({"ok": False, "error": "用户名长度需在 3-30 字符之间"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "密码长度至少需要 6 个字符"}), 400

    db = get_db()
    try:
        cur = db.cursor()
        # 检查用户名是否已存在
        cur.execute("SELECT id FROM users WHERE username=%s", (username,))
        if cur.fetchone():
            return jsonify({"ok": False, "error": "该用户名已被注册"}), 400

        # 如果提供了邀请码，校验邀请码
        inv_row = None
        if invite_code:
            cur.execute("SELECT id, used_by, is_active FROM invite_codes WHERE code=%s", (invite_code,))
            inv_row = cur.fetchone()
            if not inv_row or not inv_row.get("is_active") or inv_row.get("used_by"):
                return jsonify({"ok": False, "error": "邀请码无效或已被使用"}), 400

        pw_hash = hash_password(password)
        cur.execute(
            "INSERT INTO users (username, password, role, status, points, created_at, updated_at) VALUES (%s, %s, 'user', 1, 100, NOW(), NOW())",
            (username, pw_hash),
        )
        new_uid = cur.lastrowid

        if inv_row:
            cur.execute("UPDATE invite_codes SET used_by=%s, used_at=NOW(), is_active=0 WHERE id=%s", (new_uid, inv_row["id"]))

        db.commit()

        # 注册成功后自动登录
        session["user_id"] = new_uid
        session["username"] = username
        session["role"] = "user"
        session["is_admin"] = False
        session["is_superadmin"] = False
        session.permanent = True
        session.modified = True

        next_url = _safe_next_url(data.get("next") or request.args.get("next") or "/profile")
        return jsonify({
            "ok": True,
            "message": "注册成功并已自动登录",
            "user_id": new_uid,
            "username": username,
            "role": "user",
            "redirect": next_url
        })
    finally:
        db.close()
