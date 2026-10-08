import { describe, expect, it } from 'vitest';
import { isFetchOf, REPO } from '../src/github-rules';

const req = (method: string, url: string) => new Request(url, { method });
const REPO_NAME = 'felix-run/felix';

describe('the github.com intercept, granted one repository', () => {
  it('allows git’s fetch of that repository, and only that', () => {
    for (const url of [
      'https://github.com/felix-run/felix.git/info/refs?service=git-upload-pack',
      'https://github.com/felix-run/felix/info/refs?service=git-upload-pack',
      'https://github.com/Felix-Run/Felix.git/info/refs?service=git-upload-pack',
    ]) {
      expect(isFetchOf(req('GET', url), REPO_NAME), url).toBe(true);
    }
    expect(isFetchOf(req('POST', 'https://github.com/felix-run/felix.git/git-upload-pack'), REPO_NAME)).toBe(true);
  });

  it('refuses a push, another repository, the API and anything not plain HTTPS to github.com', () => {
    const refused: [string, string][] = [
      ['GET', 'https://github.com/felix-run/felix.git/info/refs?service=git-receive-pack'],
      ['POST', 'https://github.com/felix-run/felix.git/git-receive-pack'],
      ['GET', 'https://github.com/felix-run/other.git/info/refs?service=git-upload-pack'],
      ['POST', 'https://github.com/felix-run/felix-web.git/git-upload-pack'],
      ['GET', 'https://github.com/felix-run/felix.git/info/refs?service=git-upload-pack&x=1'],
      ['POST', 'https://github.com/felix-run/felix.git/git-upload-pack?service=x'],
      ['GET', 'https://github.com/felix-run/felix.git/info/refs'],
      ['GET', 'https://github.com/felix-run/felix'],
      ['GET', 'https://api.github.com/repos/felix-run/felix'],
      ['GET', 'http://github.com/felix-run/felix.git/info/refs?service=git-upload-pack'],
      ['GET', 'https://github.com:8443/felix-run/felix.git/info/refs?service=git-upload-pack'],
      ['GET', 'https://github.com/felix-run/felix/../other/info/refs?service=git-upload-pack'],
      ['PUT', 'https://github.com/felix-run/felix.git/git-upload-pack'],
    ];
    for (const [method, url] of refused) {
      expect(isFetchOf(req(method, url), REPO_NAME), `${method} ${url}`).toBe(false);
    }
  });

  it('takes only owner/name as a repository', () => {
    for (const ok of ['felix-run/felix', 'a/b.c_d-e']) expect(REPO.test(ok), ok).toBe(true);
    for (const bad of ['felix', '/felix', 'a/b/c', '-a/b', 'a/../b', 'a/b c', '']) expect(REPO.test(bad), bad).toBe(false);
  });
});
