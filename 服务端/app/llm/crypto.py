# -*- coding: utf-8 -*-
"""API Key 的可逆加密。

【为什么必须改掉原来的做法】
原来 asset.py 里是这么存的：

    cipher = "enc:" + hashlib.sha256(plain.encode()).hexdigest()

那是**哈希，不是加密**——取不回明文。只要存成这样，
「测试连通」这个功能在原设计下就不可能做出来：调厂商要带 Authorization,
而我们手上只有一个单向摘要。所以这里换成 Fernet（AES-128-CBC + HMAC）。

【密钥从哪来】
按优先级：
  1. 环境变量 FJHY_LLM_KEY_SECRET（部署时必须走这条）
  2. 本地文件 服务端/var/llm_secret.key（没有就生成一个，仅供本地 POC）

第 2 条是**本地便利，不是方案**：密钥和密文放在同一台机器的同一个目录下，
拿到磁盘就等于拿到 Key。生产必须走环境变量或 KMS。
这一点会在 /api/sys/llm-key-status 接口里如实回报，不藏着。

【旧数据怎么办】
库里可能已经有 "enc:" 开头的旧值（哈希）。decrypt 遇到它**不抛异常、
也不假装成功**，而是返回 None 并给出明确原因：那条配置得重新填一次 Key。
静默返回空串会让调用方带着空 Authorization 去请求，
厂商回 401，用户看到的是「Key 错了」——而真实原因是存储格式变了。
"""
import io
import os
import stat

from cryptography.fernet import Fernet, InvalidToken

from app import config

PREFIX = "fer1:"
LEGACY_PREFIX = "enc:"          # 旧的 sha256 摘要，不可逆
SECRET_FILE = os.path.join(config.SERVER_DIR, "var", "llm_secret.key")

_cached = None                  # (Fernet, 来源说明)


def _from_env():
    v = (os.environ.get("FJHY_LLM_KEY_SECRET") or "").strip()
    return (v, "环境变量 FJHY_LLM_KEY_SECRET") if v else (None, None)


def _from_file():
    if os.path.exists(SECRET_FILE):
        with io.open(SECRET_FILE, encoding="ascii") as f:
            v = f.read().strip()
        if v:
            return v, "本地文件 var/llm_secret.key（仅 POC，生产不可用）"
    return None, None


def _make_file():
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    key = Fernet.generate_key().decode("ascii")
    with io.open(SECRET_FILE, "w", encoding="ascii", newline="") as f:
        f.write(key)
    try:
        # 尽量收紧权限。Windows 上 chmod 作用有限，所以这只是尽力而为，
        # 不是保护——真正的保护是别在生产用这条路径。
        os.chmod(SECRET_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return key, "本地文件 var/llm_secret.key（本次新生成，仅 POC）"


def _fernet():
    global _cached
    if _cached is not None:
        return _cached
    for getter in (_from_env, _from_file):
        v, src = getter()
        if v:
            try:
                _cached = (Fernet(v.encode("ascii")), src)
                return _cached
            except (ValueError, TypeError) as e:
                raise RuntimeError(
                    "FJHY_LLM_KEY_SECRET / var/llm_secret.key 不是合法的 Fernet 密钥"
                    "（要求 urlsafe base64 的 32 字节）：%s。"
                    "**没有退回明文存储这条路**——宁可存不进去，也不能把 Key 裸存。" % e)
    v, src = _make_file()
    _cached = (Fernet(v.encode("ascii")), src)
    return _cached


def secret_source():
    """密钥来源的人类可读说明，给 /llm-key-status 接口如实回报。"""
    try:
        return {"ok": True, "source": _fernet()[1],
                "production_ready": bool(_from_env()[0]),
                "note": "" if _from_env()[0] else
                        "当前密钥和密文在同一台机器上，拿到磁盘即可解密。"
                        "部署时请注入 FJHY_LLM_KEY_SECRET。"}
    except RuntimeError as e:
        return {"ok": False, "source": None, "production_ready": False,
                "note": str(e)}


def encrypt(plain):
    """明文 → "fer1:<token>"。空值返回 None（而不是空密文）。"""
    if not plain:
        return None
    return PREFIX + _fernet()[0].encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(cipher):
    """密文 → (明文, 失败原因)。两者恰有一个为 None。

    失败原因写成能直接给用户看的话。「解密失败」四个字帮不了任何人判断
    下一步该做什么；「这条是旧格式，请重新填一次 Key」可以。
    """
    if not cipher:
        return None, "这条配置还没填 API Key"
    if cipher.startswith(LEGACY_PREFIX):
        return None, ("这条配置的 Key 是旧格式（单向摘要，取不回明文），"
                      "请重新填一次 API Key 再测试")
    if not cipher.startswith(PREFIX):
        return None, "Key 的存储格式无法识别（既不是 fer1: 也不是旧的 enc:）"
    try:
        return _fernet()[0].decrypt(cipher[len(PREFIX):].encode("ascii")).decode("utf-8"), None
    except InvalidToken:
        return None, ("Key 解不开——通常是加密密钥换了"
                      "（换过 FJHY_LLM_KEY_SECRET，或删过 var/llm_secret.key）。"
                      "请重新填一次 API Key")
    except RuntimeError as e:
        return None, str(e)
