# Diadromous fish monitoring stations

Saved 2026-10-18 for Hackachusetts.

MassGIS diadromous fish monitoring stations. Street location and coordinates are left out.

Rows in data.csv: 29

Columns: river, station_type, town, year_installed, temp_logger, water_chemistry

How to use it

- Spreadsheets: open data.csv in Excel or Google Sheets.
- Fresh copy: run `python fetch.py` or `node fetch.js`. Both call the URL below and save the response.
- The preview on the Hackachusetts site is a few rows of this same table.

Direct request

https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/DiadromousFish/MapServer/0/query?where=1%3D1&outFields=RIVER,STATION_TYPE,TOWN,YEAR_INSTALLED,HAS_TEMP_LOGGER,HAS_WATER_CHEMISTRY_STATION&returnGeometry=false&f=json

Check the source license before publishing a project.
