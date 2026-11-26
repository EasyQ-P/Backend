"""Object storage for teaching material originals (S3 in production, local directory for development)."""
from pathlib import Path



class LocalStorage:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root) or p == self.root:
            raise ValueError('Invalid object key')
        return p

    def put(self, key, content, media):
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def get(self, key):
        return self.path(key).read_bytes()

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)


class S3Storage:
    def __init__(self, bucket):
        import boto3
        if not bucket:
            raise RuntimeError('S3_BUCKET is required')
        self.client, self.bucket = boto3.client('s3'), bucket

    def put(self, key, content, media):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=media, ServerSideEncryption='AES256')

    def get(self, key):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with response['Body'] as body:
            return body.read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)


def storage_from(settings):
    if settings.storage_backend == 'local':
        return LocalStorage(settings.storage_root)
    if settings.storage_backend == 's3':
        return S3Storage(settings.s3_bucket)
    raise RuntimeError('STORAGE_BACKEND must be local or s3')
