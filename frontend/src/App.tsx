import { MapContainer, TileLayer } from 'react-leaflet';
import { DATA_DIR } from './data/dataDir';

// Placeholder shell. MapView, RankedTable, etc. land in src/components/ in later tasks.
export default function App() {
  return (
    <main>
      <h1>MA Tidal Buoy Siting Model</h1>
      <p>Data source: {DATA_DIR}</p>
      <MapContainer center={[41.7, -70.5]} zoom={8} style={{ height: '70vh' }}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
      </MapContainer>
    </main>
  );
}
