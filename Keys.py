import os
from cryptography.hazmat.primitives.ciphers import algorithms

def generate_aes_key_iv():
    # AES-128 key: 16 bytes
    key = os.urandom(16)
    # CBC IV: also 16 bytes
    iv = os.urandom(16)

    print("AES-128 Key (hex):", key.hex())
    print("AES CBC IV (hex):", iv.hex())

    return key, iv

if __name__ == "__main__":
    key, iv = generate_aes_key_iv()
