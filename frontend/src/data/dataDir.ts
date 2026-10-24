export const DEFAULT_DATA_DIR = '/output';
export const SAMPLE_DATA_DIR = '/output/sample';

/** Normalize a configured data dir: empty/undefined → default, strip trailing slashes. */
export function resolveDataDir(raw: string | undefined): string {
  const trimmed = (raw ?? '').trim();
  if (trimmed === '') return DEFAULT_DATA_DIR;
  const noTrailing = trimmed.replace(/\/+$/, '');
  return noTrailing === '' ? '/' : noTrailing;
}

/** Data dir from VITE_DATA_DIR (default /output; /output/sample for sample mode). */
export const DATA_DIR = resolveDataDir(import.meta.env.VITE_DATA_DIR);

/** URL of a Static_Output file, e.g. dataUrl('cells.geojson'). */
export function dataUrl(file: string, dir: string = DATA_DIR): string {
  return `${dir === '/' ? '' : dir}/${file.replace(/^\/+/, '')}`;
}
