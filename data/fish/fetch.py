"""Save a fresh copy of this Hackachusetts starter source."""
import pathlib
import urllib.parse
import urllib.request

URL = "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/DiadromousFish/MapServer/0/query?where=1%3D1&outFields=RIVER,STATION_TYPE,TOWN,YEAR_INSTALLED,HAS_TEMP_LOGGER,HAS_WATER_CHEMISTRY_STATION&returnGeometry=false&f=json"
request = urllib.request.Request(URL, headers={"User-Agent": "HackachusettsKit"})
with urllib.request.urlopen(request, timeout=90) as response:
    payload = response.read()
suffix = pathlib.Path(urllib.parse.urlparse(URL).path).suffix
path = pathlib.Path("fresh" + suffix) if suffix in {".csv", ".txt", ".json"} else pathlib.Path("fresh.json")
path.write_bytes(payload)
print(f"wrote {path} ({len(payload)} bytes)")
