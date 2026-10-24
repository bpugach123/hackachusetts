# eMOLT bottom temperature

Saved 2026-10-18 for Hackachusetts.

Vessel-collected bottom temperature for 1–7 Aug 2024. The live call returns that same window as CSV.

Rows in data.csv: 20000

Columns: time, latitude, longitude, depth_m, temperature_c

How to use it

- Spreadsheets: open data.csv in Excel or Google Sheets.
- Fresh copy: run `python fetch.py` or `node fetch.js`. Both call the URL below and save the response.
- The preview on the Hackachusetts site is a few rows of this same table.

Direct request

https://erddap.ondeckdata.com/erddap/tabledap/emolt_data.csv?time,latitude,longitude,depth,temperature&time%3E=2024-08-01T00:00:00Z&time%3C=2024-08-08T00:00:00Z

Check the source license before publishing a project.
