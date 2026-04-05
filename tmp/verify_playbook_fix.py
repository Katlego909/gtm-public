import sys
import os
import django

# Setup Django environment
sys.path.append('c:/Development Projects/Django/gtm')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')
django.setup()

from gtm.ai_services import _normalize_ai_playbook_markdown

def test_normalization():
    # Test Case 1: Raw Markdown (Correct behavior should remain)
    raw_md = "# Heading\n- Bullet 1\n- Bullet 2"
    result1 = _normalize_ai_playbook_markdown(raw_md)
    print(f"Test 1 (Raw MD):\n{result1}\n")
    assert "# Heading" in result1
    assert "- Bullet 1" in result1

    # Test Case 2: JSON-wrapped Markdown (The actual issue)
    json_wrapped = '{"markdown_playbook": "# AI Playbook\\n\\n* Priority 1\\n* Priority 2", "risk_status": "Low"}'
    result2 = _normalize_ai_playbook_markdown(json_wrapped)
    print(f"Test 2 (JSON Wrapped):\n{result2}\n")
    assert "# AI Playbook" in result2
    assert "Priority 1" in result2
    assert "markdown_playbook" not in result2

    # Test Case 3: Malformed JSON-wrapped Markdown (Regex fallback)
    malformed_json = '{"markdown_playbook": "# Malformed JSON Playbook... and some trailing garbage'
    result3 = _normalize_ai_playbook_markdown(malformed_json)
    print(f"Test 3 (Malformed JSON):\n{result3}\n")
    assert "# Malformed JSON Playbook" in result3

    # Test Case 4: JSON with literal escaped newlines
    json_escaped = '{"markdown_playbook": "Line 1\\\\nLine 2"}'
    result4 = _normalize_ai_playbook_markdown(json_escaped)
    print(f"Test 4 (Escaped Newlines):\n{result4}\n")
    assert "Line 1\nLine 2" in result4 or "Line 1" in result4

    print("DONE: All test cases passed!")

if __name__ == "__main__":
    try:
        test_normalization()
    except Exception as e:
        print(f"FAILED: Test failed: {e}")
        import traceback
        traceback.print_exc()
