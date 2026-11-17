import argparse
from pathlib import Path
from typing import List, Optional

from tqdm import tqdm

from . import config
from .pipeline import is_supported, output_name, translate_file
from .translator import Translator, describe_error


def discover_input_files(input_dir: Path) -> List[Path]:
    if not input_dir.exists():
        return []
    return sorted(p for p in input_dir.iterdir() if p.is_file() and is_supported(p.name))


def run(
    input_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    input_path = Path(input_dir or config.INPUT_DIR)
    output_path = Path(output_dir or config.OUTPUT_DIR)
    output_path.mkdir(parents=True, exist_ok=True)

    files = discover_input_files(input_path)
    if not files:
        print(f"Không tìm thấy file nào (.txt/.pdf/.epub) trong '{input_path}/'.")
        return

    translator = Translator(model=model)

    for file_path in files:
        out_file = output_path / output_name(file_path.name)
        print(f"\n=== {file_path.name} -> {out_file.name} ===")
        session = translator.session()
        with tqdm(desc=f"Dịch {file_path.name}", unit="đoạn") as bar:

            def progress(done: int, total: int, preview: str = "") -> None:
                bar.total = total
                bar.n = done
                bar.refresh()

            try:
                translate_file(file_path, out_file, session, progress)
            except Exception as exc:
                bar.close()
                print(f"Lỗi khi dịch {file_path.name}: {describe_error(exc)}")
                continue

        print(f"Xong: {out_file}")
        if session.cached_requests:
            print(f"  {session.cached_requests} đoạn lấy lại từ lần chạy trước (không tốn token).")
        print(f"  Token: {session.input_tokens:,} vào / {session.output_tokens:,} ra")
        for warning in session.warnings:
            print(f"  Cảnh báo: {warning}")


def serve(host: str, port: int) -> None:
    import uvicorn

    from .web.app import create_app

    print(f"Mở trình duyệt tại http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dịch sách (.txt/.pdf/.epub) sang tiếng Việt bằng OpenAI API"
    )
    parser.add_argument("--input", "-i", default=None, help="Thư mục chứa sách gốc (mặc định: input/)")
    parser.add_argument("--output", "-o", default=None, help="Thư mục lưu sách đã dịch (mặc định: output/)")
    parser.add_argument(
        "--model", "-m", default=None, help="Model OpenAI dùng để dịch (mặc định: gpt-4o)"
    )
    parser.add_argument("--web", action="store_true", help="Mở giao diện web thay vì dịch thư mục input/")
    parser.add_argument("--host", default="127.0.0.1", help="Địa chỉ cho giao diện web (mặc định: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Cổng cho giao diện web (mặc định: 8000)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Chạy thử: không gọi API, chỉ đánh dấu [VI] vào văn bản"
    )
    args = parser.parse_args()

    if args.dry_run:
        config.DRY_RUN = True
    if args.model:
        config.MODEL = args.model
    if args.web:
        serve(args.host, args.port)
    else:
        run(args.input, args.output, args.model)


if __name__ == "__main__":
    main()
