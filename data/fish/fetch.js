// Save a fresh copy of this Hackachusetts starter source.
// Run with: node fetch.js
const fs = require("node:fs/promises");

const url = "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/DiadromousFish/MapServer/0/query?where=1%3D1&outFields=RIVER,STATION_TYPE,TOWN,YEAR_INSTALLED,HAS_TEMP_LOGGER,HAS_WATER_CHEMISTRY_STATION&returnGeometry=false&f=json";

async function main() {
  const response = await fetch(url, { headers: { "User-Agent": "HackachusettsKit" } });
  if (!response.ok) throw new Error("Request failed: " + response.status);
  const bytes = Buffer.from(await response.arrayBuffer());
  const type = response.headers.get("content-type") || "";
  let name = "fresh.json";
  if (url.includes(".csv") || type.includes("csv")) name = "fresh.csv";
  if (url.endsWith(".txt")) name = "fresh.txt";
  await fs.writeFile(name, bytes);
  console.log("wrote " + name + " (" + bytes.length + " bytes)");
}

main();
