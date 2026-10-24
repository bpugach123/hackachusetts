# NOAA Boston tide gauge 8443970

Saved 2026-10-18 for Hackachusetts.

Hourly water level at Boston station 8443970 for August 2024. Datum is MLLW. Units are meters. The live call returns that same window as JSON.

Rows in data.csv: 744

Columns: time_gmt, water_level_m

How to use it

- Spreadsheets: open data.csv in Excel or Google Sheets.
- Fresh copy: run `python fetch.py` or `node fetch.js`. Both call the URL below and save the response.
- The preview on the Hackachusetts site is a few rows of this same table.

Direct request

https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?begin_date=20240801&end_date=20240831&station=8443970&product=hourly_height&datum=MLLW&units=metric&time_zone=gmt&format=json

Check the source license before publishing a project.
