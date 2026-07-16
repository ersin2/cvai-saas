from pdfminer.high_level import extract_text

try:
    text = extract_text('test_out.pdf')
    print("EXTRACTED TEXT:", repr(text))
except Exception as e:
    print("Error:", e)
