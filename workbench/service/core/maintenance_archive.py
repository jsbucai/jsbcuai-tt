"""Streaming, authenticated cold archives; verify before dropping local copies."""
import hashlib
import os
import zlib
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b'ULCOLD01'
CHUNK = 1024 * 1024


def verify_archive(path, key):
    path = Path(path)
    with path.open('rb') as source:
        prefix = source.read(len(MAGIC) + 12)
        if not prefix.startswith(MAGIC) or len(key) != 32:
            raise ValueError('归档格式或密钥无效')
        remaining = path.stat().st_size - len(prefix) - 16
        if remaining < 0:
            raise ValueError('归档不完整')
        source.seek(-16, os.SEEK_END)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(prefix[len(MAGIC):], source.read(16))).decryptor()
        decryptor.authenticate_additional_data(MAGIC)
        source.seek(len(prefix))
        decoder = zlib.decompressobj()
        digest, size = hashlib.sha256(), 0
        while remaining:
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                raise ValueError('归档内容截断')
            remaining -= len(chunk)
            compressed = decryptor.update(chunk)
            # Bound expanded buffers even for very compressible databases.
            while compressed:
                data = decoder.decompress(compressed, CHUNK)
                digest.update(data); size += len(data)
                compressed = decoder.unconsumed_tail
        decryptor.finalize()
        tail = decoder.flush()
        digest.update(tail); size += len(tail)
        if not decoder.eof or decoder.unused_data:
            raise ValueError('归档压缩流不完整')
        return {'sha256': digest.hexdigest(), 'bytes': size}


def write_archive(path, chunks, key):
    path = Path(path)
    if len(key) != 32 or path.is_symlink():
        raise ValueError('归档密钥或路径无效')
    nonce = os.urandom(12)
    encoder = zlib.compressobj(level=3)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    temporary = path.with_name('.' + path.name + '.tmp-' + os.urandom(6).hex())
    digest, size = hashlib.sha256(), 0
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(MAGIC + nonce)
            for chunk in chunks:
                digest.update(chunk); size += len(chunk)
                target.write(encryptor.update(encoder.compress(chunk)))
            target.write(encryptor.update(encoder.flush()))
            target.write(encryptor.finalize() + encryptor.tag)
            target.flush(); os.fsync(target.fileno())
        expected = {'sha256': digest.hexdigest(), 'bytes': size}
        if verify_archive(temporary, key) != expected:
            raise ValueError('归档校验不一致，保留源数据')
        if path.exists():
            raise ValueError('归档已存在，不覆盖')
        os.replace(temporary, path)
        return {**expected, 'archive': path.name, 'archive_bytes': path.stat().st_size}
    finally:
        temporary.unlink(missing_ok=True)


def file_chunks(path):
    with Path(path).open('rb') as source:
        while chunk := source.read(CHUNK):
            yield chunk


def restore_archive(path, destination, key):
    """Restore into a new file only; never replace a live database."""
    path, destination = Path(path), Path(destination)
    verify_archive(path, key)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as out, path.open('rb') as src:
            prefix = src.read(len(MAGIC) + 12)
            remaining = path.stat().st_size - len(prefix) - 16
            src.seek(-16, os.SEEK_END)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(prefix[len(MAGIC):], src.read(16))).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            src.seek(len(prefix)); decoder = zlib.decompressobj()
            while remaining:
                chunk = src.read(min(CHUNK, remaining))
                if not chunk: raise ValueError('归档截断')
                remaining -= len(chunk)
                compressed = decryptor.update(chunk)
                while compressed:
                    out.write(decoder.decompress(compressed, CHUNK))
                    compressed = decoder.unconsumed_tail
            decryptor.finalize(); out.write(decoder.flush())
            if not decoder.eof: raise ValueError('归档不完整')
            out.flush(); os.fsync(out.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Decrypt cold archive into a new file, never a live database')
    parser.add_argument('archive', type=Path); parser.add_argument('destination', type=Path)
    parser.add_argument('--key-file', type=Path, required=True)
    args = parser.parse_args()
    restore_archive(args.archive, args.destination, args.key_file.read_bytes())
    print('归档已恢复到新文件；未修改运行数据库')
