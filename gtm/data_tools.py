"""Deterministic analysis of uploaded data files (CSV / Excel evidence).

Agents never do arithmetic themselves: they choose an operation, a column,
and filters, and this module computes the number from the real rows. The
same computation backs propose_measurement, which recomputes the value from
the file rather than accepting one from the model -- so a proposed
measurement is always traceable to data, never an estimate.

Files are read through Django storage (GCS in production), capped in size
and rows. No eval, no arbitrary code: only the fixed operations below.
"""

import csv
import io
import statistics
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50_000
DATA_FILE_TYPES = ("csv", "xlsx")
OPERATIONS = ("count", "ratio", "sum", "mean", "median", "min", "max")
FILTER_OPS = ("eq", "ne", "contains", "not_contains", "gt", "gte", "lt", "lte", "is_empty", "not_empty")


class DataFilter(BaseModel):
    column: str
    op: str = "eq"
    value: str = ""


class DataError(Exception):
    """A readable reason the computation can't be done (bad column, file)."""


# ================================================================
# Finding and reading files
# ================================================================

def data_documents(session=None, workspace=None) -> List[Any]:
    """CSV/XLSX evidence for `workspace` (all its assessments) or `session`."""
    from .models import CategoryDocument, DeliveryDocument

    docs: List[Any] = []
    for model in (DeliveryDocument, CategoryDocument):
        qs = model.objects.filter(file_type__in=DATA_FILE_TYPES)
        if workspace is not None:
            qs = qs.filter(session__workspace=workspace)
        elif session is not None:
            qs = qs.filter(session=session)
        else:
            continue
        docs += list(qs.select_related("session").order_by("-created_at")[:50])
    return docs


def find_document(file_id: str, session=None, workspace=None):
    for doc in data_documents(session=session, workspace=workspace):
        if str(doc.pk) == str(file_id).strip():
            return doc
    raise DataError(f"No CSV or Excel file with ID {file_id} here. Use list_data_files to see the available files.")


def load_rows(doc) -> Tuple[List[str], List[Dict[str, Any]]]:
    """(columns, rows) from a CSV/XLSX evidence document."""
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        raise DataError(f"{doc.original_filename} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    try:
        with doc.file.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except (FileNotFoundError, OSError, ValueError) as e:
        raise DataError(f"Couldn't open {doc.original_filename}: {e}") from e
    if len(raw) > MAX_FILE_BYTES:
        raise DataError(f"{doc.original_filename} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if doc.file_type == "xlsx":
        return _read_xlsx(raw)
    return _read_csv(raw)


def _read_csv(raw: bytes):
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    return _rows_from_table(reader)


def _read_xlsx(raw: bytes):
    import openpyxl

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as e:
        raise DataError(f"Couldn't read the spreadsheet: {e}") from e
    try:
        return _rows_from_table(workbook.active.iter_rows(values_only=True))
    finally:
        workbook.close()


def _rows_from_table(table):
    columns: Optional[List[str]] = None
    rows: List[Dict[str, Any]] = []
    for raw_row in table:
        values = ["" if v is None else v for v in raw_row]
        if columns is None:
            if not any(str(v).strip() for v in values):
                continue
            columns = _unique_headers(values)
            continue
        if not any(str(v).strip() for v in values):
            continue
        if len(rows) >= MAX_ROWS:
            break
        rows.append({col: (values[i] if i < len(values) else "") for i, col in enumerate(columns)})
    if columns is None:
        raise DataError("The file has no header row.")
    return columns, rows


def _unique_headers(values) -> List[str]:
    headers, seen = [], {}
    for i, v in enumerate(values):
        name = str(v).strip() or f"Column {i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        headers.append(name)
    return headers


# ================================================================
# Values, filters, operations
# ================================================================

def to_number(value: Any) -> Optional[float]:
    """1,234 / $1,200.50 / 45% / (300) -> float; None if not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace(",", "").replace(" ", "")
    for symbol in ("$", "€", "£", "R", "%"):
        cleaned = cleaned.replace(symbol, "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def _to_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for candidate in (text[:19], text[:10]):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def _resolve_column(columns: List[str], name: str) -> str:
    wanted = (name or "").strip().lower()
    for col in columns:
        if col.lower() == wanted:
            return col
    raise DataError(f"There's no column '{name}'. Columns are: {', '.join(columns)}.")


def _compare(cell: Any, op: str, value: str) -> bool:
    if op == "is_empty":
        return str(cell).strip() == ""
    if op == "not_empty":
        return str(cell).strip() != ""
    if op in ("eq", "ne", "contains", "not_contains"):
        a, b = str(cell).strip().lower(), str(value).strip().lower()
        return {"eq": a == b, "ne": a != b, "contains": b in a, "not_contains": b not in a}[op]
    # Ordered comparisons: numbers first, then dates.
    left, right = to_number(cell), to_number(value)
    if left is None or right is None:
        left, right = _to_date(cell), _to_date(value)
        if left is None or right is None:
            return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


def _apply_filters(columns, rows, filters) -> List[Dict[str, Any]]:
    parsed = []
    for f in filters or []:
        f = f if isinstance(f, DataFilter) else DataFilter(**f)
        if f.op not in FILTER_OPS:
            raise DataError(f"Unknown filter op '{f.op}'. Use one of: {', '.join(FILTER_OPS)}.")
        parsed.append((_resolve_column(columns, f.column), f.op, f.value))
    return [r for r in rows if all(_compare(r[col], op, val) for col, op, val in parsed)]


def _describe_filters(filters) -> str:
    parts = []
    for f in filters or []:
        f = f if isinstance(f, DataFilter) else DataFilter(**f)
        parts.append(f"{f.column} {f.op} {f.value!r}" if f.op not in ("is_empty", "not_empty") else f"{f.column} {f.op}")
    return " and ".join(parts) or "all rows"


def compute(doc, operation: str, column: str = "", filters=None, denominator_filters=None) -> Dict[str, Any]:
    """Run one operation over a file's rows. `ratio` is the percentage of
    rows matching `filters` among rows matching `denominator_filters`.
    Returns {value, description, matched_rows, total_rows, ...}."""
    if operation not in OPERATIONS:
        raise DataError(f"Unknown operation '{operation}'. Use one of: {', '.join(OPERATIONS)}.")
    columns, rows = load_rows(doc)
    matched = _apply_filters(columns, rows, filters)
    result: Dict[str, Any] = {"operation": operation, "total_rows": len(rows), "matched_rows": len(matched)}
    where = _describe_filters(filters)

    if operation == "count":
        result["value"] = float(len(matched))
        result["description"] = f"count of rows where {where} in {doc.original_filename}"
    elif operation == "ratio":
        base = _apply_filters(columns, rows, denominator_filters)
        if not base:
            raise DataError("No rows match the denominator filters, so the ratio is undefined.")
        matched_ids = {id(r) for r in matched}
        numerator = [r for r in base if id(r) in matched_ids]
        result["value"] = round(len(numerator) / len(base) * 100, 2)
        result["denominator_rows"] = len(base)
        result["description"] = (
            f"{len(numerator)} of {len(base)} rows ({_describe_filters(denominator_filters)}) "
            f"have {where}, in {doc.original_filename}"
        )
    else:
        col = _resolve_column(columns, column)
        numbers = [n for n in (to_number(r[col]) for r in matched) if n is not None]
        result["skipped_non_numeric"] = len(matched) - len(numbers)
        if not numbers:
            raise DataError(f"No numeric values in '{col}' for rows where {where}.")
        fn = {"sum": sum, "mean": statistics.fmean, "median": statistics.median, "min": min, "max": max}[operation]
        result["value"] = round(float(fn(numbers)), 2)
        result["description"] = f"{operation} of {col} over {len(numbers)} rows where {where}, in {doc.original_filename}"
    return result


def profile(doc, sample_rows: int = 3) -> str:
    """Readable summary: columns with inferred type, example and distinct values."""
    columns, rows = load_rows(doc)
    lines = [f"{doc.original_filename}: {len(rows)} rows, {len(columns)} columns."]
    for col in columns:
        values = [r[col] for r in rows if str(r[col]).strip() != ""]
        numeric = sum(1 for v in values if to_number(v) is not None)
        dates = sum(1 for v in values[:200] if _to_date(v) is not None)
        kind = "number" if values and numeric / len(values) > 0.9 else ("date" if values and dates > len(values[:200]) * 0.9 else "text")
        distinct = sorted({str(v).strip() for v in values})
        desc = f"- {col} ({kind}, {len(values)} filled"
        if kind == "text" and len(distinct) <= 12:
            desc += f"; values: {', '.join(distinct)}"
        elif kind == "number":
            nums = [to_number(v) for v in values if to_number(v) is not None]
            desc += f"; min {min(nums):g}, max {max(nums):g}"
        else:
            desc += f"; e.g. {', '.join(distinct[:sample_rows])}"
        lines.append(desc + ")")
    return "\n".join(lines)


# ================================================================
# Agent tools
# ================================================================

def build_data_tools(session=None, workspace=None) -> List[Any]:
    """Read-only data tools over the CSV/XLSX evidence of `workspace` (all
    assessments) or `session`."""

    def list_data_files() -> str:
        """List the CSV and Excel files uploaded as evidence (deal exports,
        funnel data, support tickets...), with their IDs. Use this before
        profiling or computing anything from data.
        """
        docs = data_documents(session=session, workspace=workspace)
        if not docs:
            return "No CSV or Excel files have been uploaded yet."
        lines = ["Data files:"]
        for d in docs:
            area = d.get_category_display() if hasattr(d, "get_category_display") else "Delivery"
            lines.append(f"- [{d.pk}] {d.original_filename} ({area}, uploaded {d.created_at:%b %d, %Y})")
        return "\n".join(lines)

    def profile_data_file(file_id: str) -> str:
        """Show a data file's columns: type, how many rows are filled, the
        distinct values of short text columns, and numeric ranges. Always do
        this before compute_metric_from_file so you use real column names
        and values.
        """
        try:
            return profile(find_document(file_id, session=session, workspace=workspace))
        except DataError as e:
            return str(e)

    def compute_metric_from_file(
        file_id: str, operation: str, column: str = "", filters: Optional[list[DataFilter]] = None,
        denominator_filters: Optional[list[DataFilter]] = None,
    ) -> str:
        """Compute a number from a data file's real rows -- never estimate
        one yourself. `operation` is one of: count, ratio, sum, mean,
        median, min, max. `column` is required for sum/mean/median/min/max.
        `filters` keep only matching rows; each has `column`, `op` (eq, ne,
        contains, not_contains, gt, gte, lt, lte, is_empty, not_empty) and
        `value`; gt/lt compare numbers or dates. For `ratio`, the result is
        the percentage of rows matching `filters` among rows matching
        `denominator_filters` -- e.g. win rate = stage eq "Closed Won" among
        stage contains "Closed".
        """
        try:
            doc = find_document(file_id, session=session, workspace=workspace)
            result = compute(doc, operation, column, filters, denominator_filters)
        except DataError as e:
            return str(e)
        extra = f" ({result['skipped_non_numeric']} non-numeric rows skipped)" if result.get("skipped_non_numeric") else ""
        return f"{result['value']:g} -- {result['description']}{extra}."

    return [list_data_files, profile_data_file, compute_metric_from_file]
