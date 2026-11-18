"""Импорт таблиц: годы в заголовках столбцов и объединённые ячейки."""
from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from localcoach import importer, timeline, util  # noqa: E402

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
RNS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def make_xlsx(path: Path, rows: dict[int, dict[str, str]], merges: list[str]) -> None:
    """Минимальный .xlsx: {номер строки: {"B": "текст"}} и список объединений «B3:D3»."""
    xml_rows = "".join(
        f'<row r="{n}">' + "".join(f'<c r="{col}{n}" t="inlineStr"><is><t>{v}</t></is></c>' for col, v in cells.items())
        + "</row>" for n, cells in sorted(rows.items()))
    merge_xml = (f'<mergeCells count="{len(merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in merges)
                 + "</mergeCells>") if merges else ""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {NS} {RNS}><sheets><sheet name="Жизнь" sheetId="1" r:id="rId1"/>'
                                      "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {NS}><sheetData>{xml_rows}</sheetData>{merge_xml}</worksheet>")


class ImporterTest(unittest.TestCase):
    def convert(self, rows: dict[int, dict[str, str]], merges: list[str]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bio.xlsx"
            make_xlsx(path, rows, merges)
            return importer.to_markdown(path)

    def test_years_as_columns_with_title_and_merges(self) -> None:
        md = self.convert({
            1: {"A": "Моя биография"},
            2: {"A": "Сфера", "B": "2015", "C": "2016", "D": "2017"},
            3: {"A": "Работа", "B": "Стажёр в банке", "C": "Аналитик в банке"},
            4: {"A": "Жильё", "B": "Квартира у метро"},          # B4:D4 — три года
            6: {"A": "Учёба", "C": "Курсы английского"},
        }, ["B4:D4"])
        self.assertIn("Моя биография", md)
        self.assertIn("- Сфера: Работа; 2015: Стажёр в банке; 2016: Аналитик в банке", md)
        self.assertIn("2015–2017: Квартира у метро", md)
        self.assertIn("2016: Курсы английского", md)

    def test_years_as_rows_vertical_merge(self) -> None:
        md = self.convert({
            1: {"A": "Год", "B": "Работа", "C": "Город"},
            2: {"A": "2010", "B": "Завод", "C": "Самара"},       # C2:C4 — три года
            3: {"A": "2011", "B": "Офис"},
            4: {"A": "2012", "B": "Фриланс"},
        }, ["C2:C4"])
        self.assertIn("- Год: 2010; Работа: Завод; Город: Самара (2010–2012)", md)
        self.assertIn("- Год: 2012; Работа: Фриланс", md)

    def test_span_to_current_year_is_ongoing(self) -> None:
        y = importer.date.today().year
        md = self.convert({1: {"A": "Сфера", "B": str(y - 2), "C": str(y - 1), "D": str(y)},
                           2: {"A": "Семья", "B": "Растим сына"}, 3: {"A": "Работа", "B": "Проект"}},
                          ["B2:D2", "B3:C3"])
        self.assertIn(f"с {y - 2} по настоящее время: Растим сына", md)
        self.assertIn(f"{y - 2}–{y - 1}: Проект", md)

    def test_numeric_first_row_is_not_header(self) -> None:
        md = self.convert({1: {"A": "12", "B": "300"}, 2: {"A": "15", "B": "400"}}, [])
        self.assertIn("- 12; 300", md)

    def test_period_time_ref_starts_at_first_year(self) -> None:
        self.assertEqual([util.normalize_time_ref(v) for v in ("2015–2017", "2015-2017 гг.", "2015", "весной")],
                         ["2015", "2015", "2015", ""])

    def test_past_until_this_year_is_not_past(self) -> None:
        y = util.today()[:4]
        v = timeline._clean_verdict({"verdict": "past", "ids": ["T1"], "period": f"2015–{y}"}, {"T1"})
        self.assertEqual(v["verdict"], "unknown")
        v = timeline._clean_verdict({"verdict": "past", "ids": ["T1"], "period": "2015–2019"}, {"T1"})
        self.assertEqual(v["verdict"], "past")


if __name__ == "__main__":
    unittest.main()
