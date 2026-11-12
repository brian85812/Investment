"""由使用者執行，產生加密的 Ed25519 私鑰及可上傳富途的公開金鑰。"""
from pathlib import Path
import argparse
import getpass
import os
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def main():
    parser = argparse.ArgumentParser(description='本機產生富途行情 AppKey 金鑰；不登入、不上傳、不交易。')
    default = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'DynamicLeverage' / 'futu'
    parser.add_argument('--output-dir', type=Path, default=default)
    args = parser.parse_args()
    folder = args.output_dir.resolve()
    repo = Path(__file__).resolve().parents[1]
    if folder == repo or repo in folder.parents:
        parser.error('請使用程式倉庫外的私人資料夾，避免私鑰進入版本控制。')
    private_path, public_path = folder / 'futu_private_key.pem', folder / 'futu_public_key.pem'
    if private_path.exists() or public_path.exists():
        parser.error('金鑰檔已存在，拒絕覆寫。')
    password = getpass.getpass('設定私鑰加密密碼（不顯示、不儲存）：')
    if len(password) < 12 or password != getpass.getpass('再輸入一次密碼：'):
        parser.error('密碼須至少12字元且兩次一致。')
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(password.encode('utf-8')))
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    folder.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
        handle.write(private)
    with public_path.open('xb') as handle:
        handle.write(public)
    print(f'已產生加密私鑰：{private_path}')
    print(f'只將公開金鑰上傳至富途 Console：{public_path}')
    print('私鑰與加密密碼請留在本機／GitHub Secrets，不要貼入對話或提交Git。')


if __name__ == '__main__':
    main()
