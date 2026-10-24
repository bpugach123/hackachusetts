import { describe, expect, it } from 'vitest';
import fc from 'fast-check';
import { DEFAULT_DATA_DIR, dataUrl, resolveDataDir } from '../src/data/dataDir';

describe('resolveDataDir', () => {
  it('defaults to /output when unset or blank', () => {
    expect(resolveDataDir(undefined)).toBe(DEFAULT_DATA_DIR);
    expect(resolveDataDir('  ')).toBe('/output');
  });

  it('accepts sample mode and strips trailing slashes', () => {
    expect(resolveDataDir('/output/sample/')).toBe('/output/sample');
    expect(dataUrl('cells.geojson', '/output/sample')).toBe('/output/sample/cells.geojson');
  });

  it('never yields a trailing slash or double slash in URLs', () => {
    const seg = fc.stringMatching(/^[a-z0-9_-]{1,8}$/);
    fc.assert(
      fc.property(fc.array(seg, { minLength: 1, maxLength: 4 }), fc.nat(3), seg, (parts, slashes, file) => {
        const dir = resolveDataDir('/' + parts.join('/') + '/'.repeat(slashes));
        expect(dir.endsWith('/')).toBe(false);
        expect(dataUrl(file + '.json', dir)).not.toContain('//');
      }),
      { numRuns: 100 },
    );
  });
});
