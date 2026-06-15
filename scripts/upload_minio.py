"""Upload a local Hive-partitioned Parquet tree to MinIO/S3.

    python scripts/upload_minio.py --src ./data/trips --bucket trips --prefix trips

Reads connection details from the same env vars the engine uses
(AWS_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY). After upload,
register the dataset with uri = s3://<bucket>/<prefix>.
"""

from __future__ import annotations

import argparse
import os

import pyarrow.fs as pafs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="./data/trips")
    ap.add_argument("--bucket", default="trips")
    ap.add_argument("--prefix", default="trips")
    args = ap.parse_args()

    endpoint = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:9000")
    s3 = pafs.S3FileSystem(
        endpoint_override=endpoint,
        scheme="https" if endpoint.startswith("https") else "http",
        access_key=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region=os.environ.get("AWS_REGION", "us-east-1"),
    )

    # Ensure bucket exists.
    try:
        s3.create_dir(args.bucket)
    except Exception:
        pass

    local = pafs.LocalFileSystem()
    src = os.path.abspath(args.src)
    n = 0
    for info in local.get_file_info(pafs.FileSelector(src, recursive=True)):
        if info.type != pafs.FileType.File or not info.path.endswith(".parquet"):
            continue
        rel = info.path[len(src):].lstrip("/\\").replace("\\", "/")
        dest = f"{args.bucket}/{args.prefix}/{rel}"
        with local.open_input_file(info.path) as fin, s3.open_output_stream(dest) as fout:
            fout.write(fin.readall())
        n += 1
        print(f"uploaded {dest}")
    print(f"\nDone: {n} files -> s3://{args.bucket}/{args.prefix}")


if __name__ == "__main__":
    main()
