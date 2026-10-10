import os
import re
import json
import base64
import hashlib
import urllib.request

try:
    from Crypto.Cipher import AES
    HAS_PYCRYPTODOME = True
except ImportError:
    HAS_PYCRYPTODOME = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

UUID_REGEX = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)

def fetch_mango_rights(song_id):
    rights_url = 'https://studio-api.prod.suno.com/api/mango/rights'
    payload = json.dumps({
        "content_params": {
            "content_id": song_id,
            "content_type": "clip"
        }
    }).encode('utf-8')

    req = urllib.request.Request(
        rights_url,
        data=payload,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Content-Type': 'application/json',
            'Origin': 'https://suno.com',
            'Referer': f'https://suno.com/song/{song_id}'
        }
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))

def decrypt_suno_data(song_id, enc_data):
    rights = fetch_mango_rights(song_id)
    glt = rights['glt']
    user_key = hashlib.sha256(glt.encode('utf-8')).digest()

    wrapped_key = base64.b64decode(rights['key'])
    wrapped_iv = base64.b64decode(rights['iv'])
    aad = song_id.encode('utf-8')

    if HAS_PYCRYPTODOME:
        # 使用 pycryptodome 解密
        # GCM 解密 wrapped_key: 前 12 字节为 nonce，之后为密文与 tag(最后16字节)
        key_nonce = wrapped_key[:12]
        key_ciphertext = wrapped_key[12:-16]
        key_tag = wrapped_key[-16:]
        cipher_gcm = AES.new(user_key, AES.MODE_GCM, nonce=key_nonce)
        cipher_gcm.update(aad)
        content_key = cipher_gcm.decrypt_and_verify(key_ciphertext, key_tag)

        iv_nonce = wrapped_iv[:12]
        iv_ciphertext = wrapped_iv[12:-16]
        iv_tag = wrapped_iv[-16:]
        cipher_iv_gcm = AES.new(user_key, AES.MODE_GCM, nonce=iv_nonce)
        cipher_iv_gcm.update(aad)
        content_iv = cipher_iv_gcm.decrypt_and_verify(iv_ciphertext, iv_tag)

        # CTR 模式解密实际音频数据
        ctr_cipher = AES.new(content_key, AES.MODE_CTR, initial_value=content_iv, nonce=b'')
        return ctr_cipher.decrypt(enc_data)

    elif HAS_CRYPTOGRAPHY:
        # 使用 cryptography 解密
        aesgcm = AESGCM(user_key)
        content_key = aesgcm.decrypt(wrapped_key[:12], wrapped_key[12:], aad)
        content_iv = aesgcm.decrypt(wrapped_iv[:12], wrapped_iv[12:], aad)

        cipher = Cipher(algorithms.AES(content_key), modes.CTR(content_iv))
        decryptor = cipher.decryptor()
        return decryptor.update(enc_data) + decryptor.finalize()
    else:
        raise RuntimeError("未检测到可用的加密解密库 (pycryptodome 或 cryptography)")

def process_input_audio(input_file, temp_dir):
    """
    检查音频，如果是 Suno 加密 m4a 则自动解密，否则直接返回原文件路径
    返回: (实际可用音频路径, 是否为临时解密文件)
    """
    ext = os.path.splitext(input_file)[1].lower()
    if ext != '.m4a':
        return input_file, False

    try:
        with open(input_file, 'rb') as f:
            header = f.read(16)
        is_standard_m4a = len(header) >= 8 and header[4:8] == b'ftyp'
        if is_standard_m4a:
            return input_file, False

        m = UUID_REGEX.search(os.path.basename(input_file))
        if not m:
            return input_file, False

        song_id = m.group(0).lower()
        with open(input_file, 'rb') as f:
            enc_data = f.read()

        dec_data = decrypt_suno_data(song_id, enc_data)
        out_temp = os.path.join(temp_dir, f"decrypted_{song_id}.m4a")
        with open(out_temp, 'wb') as f:
            f.write(dec_data)

        return out_temp, True
    except Exception as e:
        print(f"[!] 尝试自动解密 Suno M4A 遇到异常: {e}，将按原始文件尝试读取")
        return input_file, False
