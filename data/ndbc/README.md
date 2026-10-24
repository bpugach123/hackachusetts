# NOAA buoy 44013, Boston

Saved 2026-10-18 for Hackachusetts.

Latest realtime observations from the Boston buoy. MM in the source means missing and is blank in data.csv.

Rows in data.csv: 6482

Columns: time, wind_dir, wind_mps, gust_mps, wave_m, pressure_hpa, air_c, water_c

How to use it

- Spreadsheets: open data.csv in Excel or Google Sheets.
- Fresh copy: run `python fetch.py` or `node fetch.js`. Both call the URL below and save the response.
- The preview on the Hackachusetts site is a few rows of this same table.

Direct request

https://www.ndbc.noaa.gov/data/realtime2/44013.txt

Check the source license before publishing a project.
