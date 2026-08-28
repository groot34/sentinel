import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app import ReportGenerator, MockPool

def test_report_generator_exception_leak():
    pool = MockPool(size=5)
    generator = ReportGenerator(pool)
    with pytest.raises(ValueError):
        generator.generate_report("INVALID_DATE")
    # In the buggy code, pool.active remains 1
    assert pool.active == 1, "Connection was leaked on ValueError as expected in baseline bug."
