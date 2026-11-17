import os

from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL = os.environ.get("TRANSLATE_MODEL", "gpt-4o")
TARGET_LANGUAGE = os.environ.get("TARGET_LANGUAGE", "Vietnamese")
TARGET_LANGUAGE_CODE = os.environ.get("TARGET_LANGUAGE_CODE", "vi")
MAX_CHARS_PER_CHUNK = int(os.environ.get("MAX_CHARS_PER_CHUNK", "6000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "16000"))
INPUT_DIR = os.environ.get("INPUT_DIR", "input")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
# Uploads, the job list and the translation cache live here.
DATA_DIR = os.environ.get("DATA_DIR", "data")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
# Demo mode: fake translations, no API calls, nothing billed.
DRY_RUN = os.environ.get("TRANSLATE_DRY_RUN", "").lower() in ("1", "true", "yes")
