# Book Translator (AI)

Công cụ Python dùng OpenAI API để tự động dịch sách (`.txt`, `.pdf`, `.epub`) sang tiếng Việt,
có giao diện web và dòng lệnh.

Lưu ý: chỉ nên dùng để dịch cho mục đích cá nhân. Nếu sách có bản quyền, không
nên phân phối lại bản dịch.

## Cài đặt

```bash
pip install -r requirements.txt
cp .env.example .env
# Mở .env và điền OPENAI_API_KEY của bạn
```

## Sử dụng

### Giao diện web

```bash
python main.py --web
```

Mở http://127.0.0.1:8000, kéo thả sách vào trang. Mỗi cuốn hiện tiến độ, đoạn vừa dịch,
và nút tải bản dịch khi xong. Sách được dịch lần lượt từng cuốn; có thể hủy, thử lại,
hoặc xóa khỏi danh sách. Bản dịch cũng được lưu trong `output/`.

### Dòng lệnh

1. Đặt các file sách (`.txt`, `.pdf`, `.epub`) vào thư mục `input/`.
2. Chạy:

```bash
python main.py
```

3. Kết quả nằm trong thư mục `output/`, mỗi file có tên `<tên-gốc>_vi.<đuôi>`.

| Flag | Mô tả |
|---|---|
| `--web` | Mở giao diện web thay vì dịch thư mục `input/` |
| `--host`, `--port` | Địa chỉ và cổng của giao diện web (mặc định `127.0.0.1:8000`) |
| `--input`, `-i` | Thư mục chứa sách gốc (mặc định `input/`) |
| `--output`, `-o` | Thư mục lưu sách đã dịch (mặc định `output/`) |
| `--model`, `-m` | Model OpenAI (mặc định `gpt-4o`; có thể đổi sang `gpt-4o-mini` để rẻ/nhanh hơn) |
| `--dry-run` | Chạy thử không gọi API: văn bản chỉ được đánh dấu `[VI]`, không tốn tiền |

## Cách hoạt động

- **`.txt`** → tách theo đoạn văn (đoạn quá dài thì cắt giữa hai câu), dịch, ghép lại thành `.txt` mới.
- **`.epub`** → dịch từng khối văn bản (đoạn văn, tiêu đề, mục danh sách) kèm thẻ định dạng
  bên trong nó (`<i>`, `<a>`, chú thích...), nên câu không bị cắt vụn. Chỉ các chương, tên sách
  và mục lục được ghi lại; CSS, ảnh, font được chép nguyên từng byte sang file mới.
- **`.pdf`** → trích văn bản, bỏ tiêu đề đầu trang và số trang lặp lại, để model dựng lại đoạn
  văn sạch, rồi xuất ra `.docx` (không giữ được layout gốc). PDF dạng scan cần OCR trước.

Để bản dịch nhất quán cả cuốn, mỗi yêu cầu gửi kèm **bảng thuật ngữ** (tên riêng, thuật ngữ,
cách xưng hô đã chốt ở các đoạn trước) và **phần cuối của bản dịch liền trước**.

Các trường hợp lỗi được xử lý thay vì bỏ qua âm thầm:

- Model trả về thiếu một khối → khối đó được dịch lại riêng.
- Bản dịch bị cắt cụt vì quá dài → đoạn được chia đôi và dịch lại.
- Thẻ liên kết/chú thích bị mất khi dịch → dịch lại, nếu vẫn hỏng thì giữ nguyên bản gốc.
- Đoạn nào phải giữ nguyên bản gốc đều được báo trong phần "Lưu ý" sau khi dịch xong.

Mỗi đoạn đã dịch được lưu trong `data/cache/`. Nếu đang dịch mà bị lỗi mạng, hết credit hay tắt
máy, chạy lại sẽ tiếp tục từ chỗ dở mà không tốn tiền cho phần đã dịch.

## Cấu hình (`.env`)

| Biến | Mặc định | Mô tả |
|---|---|---|
| `OPENAI_API_KEY` | — | API key của bạn (bắt buộc) |
| `TRANSLATE_MODEL` | `gpt-4o` | Model dùng để dịch |
| `TARGET_LANGUAGE` / `TARGET_LANGUAGE_CODE` | `Vietnamese` / `vi` | Ngôn ngữ đích và mã ngôn ngữ |
| `MAX_CHARS_PER_CHUNK` | `6000` | Kích thước tối đa mỗi đoạn gửi đi (ký tự) |
| `MAX_OUTPUT_TOKENS` | `16000` | Giới hạn token đầu ra mỗi yêu cầu |
| `INPUT_DIR` / `OUTPUT_DIR` | `input` / `output` | Thư mục vào/ra |
| `DATA_DIR` | `data` | Nơi lưu file tải lên, danh sách công việc và cache bản dịch |
| `MAX_UPLOAD_MB` | `200` | Dung lượng tối đa mỗi file tải lên qua web |
| `TRANSLATE_DRY_RUN` | — | Đặt `1` để chạy thử không gọi API |

## Kiểm thử

```bash
python -m pytest tests
```

## Cấu trúc project

```
book_translator/
  config.py             # đọc cấu hình từ .env
  common.py             # chia văn bản thành các đoạn nhỏ
  translator.py         # gọi OpenAI API, giữ bảng thuật ngữ + ngữ cảnh, cache
  pipeline.py           # chọn handler theo đuôi file
  formats/
    txt_handler.py
    pdf_handler.py
    epub_handler.py
  cli.py                # entrypoint CLI
  web/
    app.py              # API FastAPI
    jobs.py             # hàng đợi dịch (một luồng xử lý)
    static/index.html   # giao diện
tests/
main.py
input/                  # đặt sách gốc vào đây
output/                 # sách đã dịch sẽ xuất ra đây
```
