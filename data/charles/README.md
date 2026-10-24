# USGS Charles River at Waltham

Saved 2026-10-18 for Hackachusetts.

Daily mean discharge, cubic feet per second, USGS site 01104500, Charles River at Waltham, calendar year 2024. Parameter code 00060.

Rows in data.csv: 366

Columns: date, discharge_cfs

How to use it

- Spreadsheets: open data.csv in Excel or Google Sheets.
- Fresh copy: run `python fetch.py` or `node fetch.js`. Both call the URL below and save the response.
- The preview on the Hackachusetts site is a few rows of this same table.

Direct request

https://waterservices.usgs.gov/nwis/dv/?format=json&sites=01104500&parameterCd=00060&startDT=2024-01-01&endDT=2024-12-31

Check the source license before publishing a project.
