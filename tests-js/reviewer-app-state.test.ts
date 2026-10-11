/**
 * Regressions for the R2.8.1 human calibration reviewer front-end
 * (`backtest/calibration_reviewer/static/app.js`).
 *
 * The review found three UI-state defects:
 *
 * 1. The notes textarea was never re-hydrated from the loaded sample's saved
 *    draft, so one headline's notes (typed or stale) bled into the next.
 * 2. `saveAndNext()` navigated unconditionally: `saveDraft()` swallowed
 *    exceptions, so a failed save still advanced to the next headline,
 *    silently dropping the draft.
 * 3. Per-dataset ordered-ID caches were never reset on dataset switch, so
 *    navigation between datasets A/B/C could reuse another dataset's sample
 *    ids and positions.
 *
 * app.js is a browser script with no exports, so these tests load its source
 * into a sandboxed VM context with a minimal DOM/fetch stub. The tests assert
 * *runtime behavior* of the loaded module (not source regexes): each scenario
 * boots the real script, drives its functions, and checks state transitions.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'

import { describe, test } from 'vitest'

// tests-js sits inside the repo checkout: <repo>/tests-js/../backtest/...
const REPO_ROOT = path.resolve(__dirname, '..')

const APP_JS = path.join(
  REPO_ROOT,
  'backtest',
  'calibration_reviewer',
  'static',
  'app.js'
)

interface Sample {
  dataset_id: string
  status: string
  version: number
  blind: Record<string, string>
  draft: {
    category?: string
    direction?: string
    severity?: string
    ma_role?: string
    certainty?: string | null
    notes: string
    flagged: boolean
  }
  rubric_approved: boolean
  revealed: { consensus: boolean; model: boolean }
}

 
interface Harness {
  sandbox: any
  fetchLog: Array<{ path: string; init?: RequestInit }>
  samples: Map<string, Sample>
  route: (fn: (url: string, init?: RequestInit) => unknown) => void
}

/** Minimal DOM stub: only the ids app.js touches. */
function makeElements(): Map<string, Record<string, any>> {
  const ids = [
    'pairing', 'paircode', 'pairbtn', 'pairmsg',
    'app', 'ds-name', 'ds-role', 'banner',
    'pos-n', 'pos-total',
    'progress-panel', 'rubric-panel', 'rubric-status', 'rubric-body',
    'rubric-approve-state', 'rubric-note', 'btn-approve-rubric',
    'pg-ds', 'pg-locked', 'pg-canonical', 'pg-draft', 'pg-flagged',
    'pg-remaining', 'btn-progress',
    's-ticker', 's-time', 's-source', 's-headline', 'statusline',
    'opts-category', 'opts-direction', 'opts-severity', 'opts-ma_role',
    'opts-certainty',
    'notes', 'btn-prev', 'btn-next', 'btn-draft', 'btn-lock', 'btn-flag',
    'revise-panel', 'revise-reason', 'btn-revise',
    'reveal-panel', 'consensus-box', 'model-box',
    'btn-reveal-consensus', 'btn-reveal-model', 'btn-rubric',
  ]

  const map = new Map<string, Record<string, any>>()

  for (const id of ids) {
    map.set(id, {
      id,
      hidden: false,
      textContent: '',
      value: '',
      className: '',
      innerHTML: '',
      disabled: false,
      listeners: new Map<string, Array<() => void>>(),
      addEventListener(kind: string, fn: () => void) {
        this.listeners.set(
          kind, [...(this.listeners.get(kind) ?? []), fn]
        )
      },
      append(..._kids: unknown[]) {},
      appendChild(_kid: unknown) {},
      classList: {
        toggle() {},
      },
      setAttribute() {},
    })
  }

  return map
}

function harness(routes: Map<string, unknown>): Harness {
  const elements = makeElements()
  const fetchLog: Harness['fetchLog'] = []

  const sandbox: Record<string, any> = {
    console,
    setTimeout,
    window: { location: { assign() {} } },
    document: {
      getElementById(id: string) {
        return elements.get(id)
      },
      addEventListener() {},
      querySelectorAll(_sel: string) {
        return []
      },
      createElement(tag: string) {
        return {
          tagName: tag.toUpperCase(),
          textContent: '',
          className: '',
          value: '',
          dataset: {},
          listeners: new Map<string, Array<() => void>>(),
          addEventListener(kind: string, fn: () => void) {
            this.listeners.set(
              kind, [...(this.listeners.get(kind) ?? []), fn]
            )
          },
          append(..._kids: unknown[]) {},
          appendChild(_kid: unknown) {},
          classList: { toggle() {} },
          setAttribute() {},
        }
      },
    },
  }

  // Resolves routes by substring match on the request path.
  sandbox.fetch = async (reqPath: string, init?: RequestInit) => {
    fetchLog.push({ path: reqPath, init })

    for (const [needle, body] of routes) {
      if (reqPath.includes(needle)) {
        return {
          ok: true,
          status: 200,
          headers: { get: (h: string) =>
            h.toLowerCase() === 'content-type'
              ? 'application/json'
              : null },
          json: async () => body,
        }
      }
    }

    return {
      ok: false,
      status: 500,
      headers: { get: () => 'application/json' },
      json: async () => ({ error: 'unrouted: ' + reqPath }),
    }
  }

  sandbox.route_override = undefined

  const h: Harness = {
    sandbox,
    fetchLog,
    samples: new Map(),
    route(fn) {
      sandbox.fetch = async (reqPath: string, init?: RequestInit) => {
        fetchLog.push({ path: reqPath, init })

        return fn(reqPath, init)
      }
    },
  }

  return h
}

function boot(h: Harness): void {
  const context = vm.createContext(h.sandbox)
  vm.runInContext(fs.readFileSync(APP_JS, 'utf-8'), context, {
    filename: 'app.js',
  })
  h.sandbox.__context = context
}

async function call(h: Harness, fn: string, ...args: unknown[]): Promise<unknown> {
  const fnBody = h.sandbox[fn] as ((...a: unknown[]) => Promise<unknown>) | undefined
  assert.ok(typeof fnBody === 'function', `app.js must define ${fn}()`)

  return fnBody(...args)
}

/** Read a field of app.js's module-scoped `state` object. */
function getState(h: Harness, key: string): unknown {
  const probe = vm.runInContext('state', h.sandbox.__context) as
    Record<string, unknown>

  return probe ? probe[key] : undefined
}

/** Mutate a field of app.js's module-scoped `state` object. */
function setState(h: Harness, key: string, value: unknown): void {
  vm.runInContext(`state[${JSON.stringify(key)}] = ${JSON.stringify(value)}`,
    h.sandbox.__context)
}

function samplePayload(datasetId: string, sid: string, notes: string): Sample {
  return {
    dataset_id: datasetId,
    status: 'UNREVIEWED',
    version: 0,
    blind: {
      sample_id: sid,
      ord: '0',
      ticker: 'AAPL',
      published_at: '2020-01-01T00:00:00+00:00',
      source: 'TEST',
      headline_text: 'Headline for ' + sid,
      headline_hash: 'a'.repeat(64),
    },
    draft: {
      certainty: null,
      notes,
      flagged: false,
    },
    rubric_approved: false,
    revealed: { consensus: false, model: false },
  }
}

function baseRoutes(h: Harness): Map<string, unknown> {
  const routes = new Map<string, unknown>()
  routes.set('/api/session', { paired: false })
  routes.set('/api/rubric', {
    status: 'PROPOSED — PENDING HUMAN APPROVAL',
    version: 'r281-reviewer-rubric-0.1',
    sha256: 'f'.repeat(64),
    category: 'c', entity_relevance: 'e', direction: 'd', severity: 's',
    confirmation_and_evidence: 'ce', ma_role: 'm', other_vs_specific: 'o',
    unresolved: [],
  })
  routes.set('/api/progress', {
    dataset_id: 'A_CANONICAL_240', total: 2, unreviewed: 2, draft: 0,
    locked: 0, flagged: 0, remaining: 2, canonical_locked: 0,
  })
  routes.set('/api/order', {
    dataset_id: 'A_CANONICAL_240',
    sample_ids: orderIds('A_CANONICAL_240', 2),
  })
  routes.set('/api/sample', () => {
    const last = h.fetchLog.filter((f) => f.path.includes('/api/sample')).pop()

    const sid = new URL(last ? last.path : '', 'http://x')
      .searchParams.get('sample_id')

    return samplePayload('A_CANONICAL_240', sid ?? '', '')
  })
  routes.set('/api/datasets', {
    datasets: [
      { dataset_id: 'A_CANONICAL_240', display_name: 'A', role: 'CANONICAL' },
      { dataset_id: 'B_SAFETY_99', display_name: 'B', role: 'SAFETY_CHALLENGE' },
      { dataset_id: 'C_SUPPLEMENTAL_36', display_name: 'C', role: 'SAFETY_SUPPLEMENTAL' },
    ],
    enums: {
      category: ['EARNINGS', 'OTHER'],
      direction: ['BULLISH', 'NEUTRAL'],
      severity: ['LOW', 'MEDIUM'],
      ma_role: ['NEITHER'],
    },
  })

  return routes
}

function orderIds(datasetId: string, n: number): string[] {
  return Array.from({ length: n }, (_, i) =>
    `${datasetId}-${String(i).padStart(4, '0')}`
  )
}

describe('reviewer app.js sample switching', () => {
  test('loading a sample restores its saved draft notes into the textarea', async () => {
    const h = harness(new Map())
    const samples = h.samples
    samples.set(
      'A_CANONICAL_240-0000',
      samplePayload('A_CANONICAL_240', 'A_CANONICAL_240-0000', 'saved draft notes for zero')
    )
    const ids = orderIds('A_CANONICAL_240', 2)
    const routes = baseRoutes(h)
    routes.set('/api/order', { dataset_id: 'A_CANONICAL_240', sample_ids: ids })
    routes.set('/api/resume', { sample_id: ids[0] })
    routes.set('/api/sample', () => {
      // The last requested sample id decides the payload.
      const last = h.fetchLog.filter((f) => f.path.includes('/api/sample')).pop()

      const sid = new URL(last ? last.path : '', 'http://x')
        .searchParams.get('sample_id')

      return samples.get(sid ?? '') ?? samplePayload('A_CANONICAL_240', sid ?? '', '')
    })
    h.route((url) => {
      for (const [needle, body] of routes) {
        if (url.includes(needle)) {
          return {
            ok: true, status: 200,
            headers: { get: (hdr: string) =>
              hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
            json: async () => (typeof body === 'function' ? body() : body),
          }
        }
      }

      return {
        ok: false, status: 500,
        headers: { get: () => 'application/json' },
        json: async () => ({ error: 'unrouted ' + url }),
      }
    })
    boot(h)
    const el = (id: string) => h.sandbox.document.getElementById(id)
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0000')
    assert.equal(el('notes').value, 'saved draft notes for zero')
    // Switch to a different headline with empty saved notes: the textarea
    // must NOT keep the previous headline's notes.
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0001')
    assert.equal(el('notes').value, '')
  })

  test('navigating between headlines with different saved notes restores each draft', async () => {
    const h = harness(new Map())
    const samples = h.samples
    samples.set(
      'A_CANONICAL_240-0000',
      samplePayload('A_CANONICAL_240', 'A_CANONICAL_240-0000', 'notes one')
    )
    samples.set(
      'A_CANONICAL_240-0001',
      samplePayload('A_CANONICAL_240', 'A_CANONICAL_240-0001', 'notes two')
    )
    const ids = orderIds('A_CANONICAL_240', 2)
    const routes = baseRoutes(h)
    routes.set('/api/order', { dataset_id: 'A_CANONICAL_240', sample_ids: ids })
    routes.set('/api/sample', () => {
      const last = h.fetchLog.filter((f) => f.path.includes('/api/sample')).pop()

      const sid = new URL(last ? last.path : '', 'http://x')
        .searchParams.get('sample_id')

      return samples.get(sid ?? '')
    })
    h.route((url) => {
      for (const [needle, body] of routes) {
        if (url.includes(needle)) {
          return {
            ok: true, status: 200,
            headers: { get: (hdr: string) =>
              hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
            json: async () => (typeof body === 'function' ? body() : body),
          }
        }
      }

      return {
        ok: false, status: 500,
        headers: { get: () => 'application/json' },
        json: async () => ({ error: 'unrouted ' + url }),
      }
    })
    boot(h)
    const el = (id: string) => h.sandbox.document.getElementById(id)
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0000')
    assert.equal(el('notes').value, 'notes one')
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0001')
    assert.equal(el('notes').value, 'notes two')
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0000')
    assert.equal(el('notes').value, 'notes one')
  })

  test('loadSampleById clears the revision reason typed for the previous sample', async () => {
    const h = harness(new Map())
    const ids = orderIds('A_CANONICAL_240', 2)
    const routes = baseRoutes(h)
    routes.set('/api/order', { dataset_id: 'A_CANONICAL_240', sample_ids: ids })
    h.route((url) => {
      for (const [needle, body] of routes) {
        if (url.includes(needle)) {
          return {
            ok: true, status: 200,
            headers: { get: (hdr: string) =>
              hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
            json: async () => (typeof body === 'function' ? body() : body),
          }
        }
      }

      return {
        ok: false, status: 500,
        headers: { get: () => 'application/json' },
        json: async () => ({ error: 'unrouted ' + url }),
      }
    })
    boot(h)
    setState(h, 'datasetId', 'A_CANONICAL_240')
    const el = (id: string) => h.sandbox.document.getElementById(id)
    el('revise-reason').value = 'stale reason for a previous sample'
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0001')
    assert.equal(el('revise-reason').value, '')
  })
})

describe('reviewer app.js save & next gating', () => {
  test('saveAndNext does NOT navigate when the draft POST fails', async () => {
    const h = harness(new Map())
    const ids = orderIds('A_CANONICAL_240', 3)
    const routes = baseRoutes(h)
    routes.set('/api/order', { dataset_id: 'A_CANONICAL_240', sample_ids: ids })
    h.route((url) => {
      if (url.includes('/api/draft')) {
        // Server rejects the draft (e.g. stale version / validation error).
        return {
          ok: false, status: 409,
          headers: { get: (hdr: string) =>
            hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
          json: async () => ({ error: 'stale write' }),
        }
      }

      for (const [needle, body] of routes) {
        if (url.includes(needle)) {
          return {
            ok: true, status: 200,
            headers: { get: (hdr: string) =>
              hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
            json: async () => (typeof body === 'function' ? body() : body),
          }
        }
      }

      return {
        ok: false, status: 500,
        headers: { get: () => 'application/json' },
        json: async () => ({ error: 'unrouted ' + url }),
      }
    })
    boot(h)
    setState(h, 'datasetId', 'A_CANONICAL_240')
    const el = (id: string) => h.sandbox.document.getElementById(id)
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0000')
    el('notes').value = 'unsaved input that must survive a failed save'
    const samplesBefore = h.fetchLog.filter((f) => f.path.includes('/api/sample')).length
    await call(h, 'saveAndNext')
    // No navigation: no additional /api/sample fetch beyond the initial load.
    const samplesAfter = h.fetchLog.filter((f) => f.path.includes('/api/sample')).length
    assert.equal(samplesAfter, samplesBefore, 'must stay on the current headline')
    assert.match(el('statusline').textContent, /NOT saved|stale write/)
  })

  test('saveAndNext navigates after the server confirms the draft', async () => {
    const h = harness(new Map())
    const ids = orderIds('A_CANONICAL_240', 3)
    const samples = h.samples

    for (let i = 0; i < 3; i++) {
      samples.set(
        ids[i],
        samplePayload('A_CANONICAL_240', ids[i], '')
      )
    }

    const routes = baseRoutes(h)
    routes.set('/api/order', { dataset_id: 'A_CANONICAL_240', sample_ids: ids })
    routes.set('/api/draft', { version: 1, status: 'DRAFT' })
    routes.set('/api/progress', {
      dataset_id: 'A_CANONICAL_240', total: 3, unreviewed: 2, draft: 1,
      locked: 0, flagged: 0, remaining: 3, canonical_locked: 0,
    })
    routes.set('/api/sample', () => {
      const last = h.fetchLog.filter((f) => f.path.includes('/api/sample')).pop()

      const sid = new URL(last ? last.path : '', 'http://x')
        .searchParams.get('sample_id')

      return samples.get(sid ?? '')
    })
    h.route((url) => {
      for (const [needle, body] of routes) {
        if (url.includes(needle)) {
          return {
            ok: true, status: 200,
            headers: { get: (hdr: string) =>
              hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
            json: async () => (typeof body === 'function' ? body() : body),
          }
        }
      }

      return {
        ok: false, status: 500,
        headers: { get: () => 'application/json' },
        json: async () => ({ error: 'unrouted ' + url }),
      }
    })
    boot(h)
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0000')
    const samplesBefore = h.fetchLog.filter((f) => f.path.includes('/api/sample')).length
    await call(h, 'saveAndNext')
    const samplesAfter = h.fetchLog.filter((f) => f.path.includes('/api/sample')).length
    assert.equal(samplesAfter, samplesBefore + 1, 'must advance exactly one sample')
  })

  test('saveDraft returns false on failure and true on server confirmation', async () => {
    const h = harness(new Map())
    const ids = orderIds('A_CANONICAL_240', 1)
    const routes = baseRoutes(h)
    routes.set('/api/order', { dataset_id: 'A_CANONICAL_240', sample_ids: ids })
    routes.set('/api/progress', {
      dataset_id: 'A_CANONICAL_240', total: 1, unreviewed: 1, draft: 0,
      locked: 0, flagged: 0, remaining: 1, canonical_locked: 0,
    })
    h.route((url) => {
      if (url.includes('/api/draft')) {
        return {
          ok: false, status: 413,
          headers: { get: (hdr: string) =>
            hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
          json: async () => ({ error: 'notes too long' }),
        }
      }

      for (const [needle, body] of routes) {
        if (url.includes(needle)) {
          return {
            ok: true, status: 200,
            headers: { get: (hdr: string) =>
              hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
            json: async () => (typeof body === 'function' ? body() : body),
          }
        }
      }

      return {
        ok: false, status: 500,
        headers: { get: () => 'application/json' },
        json: async () => ({ error: 'unrouted ' + url }),
      }
    })
    boot(h)
    setState(h, 'datasetId', 'A_CANONICAL_240')
    await call(h, 'loadSampleById', 'A_CANONICAL_240-0000')
    const failed = await call(h, 'saveDraft')
    assert.equal(failed, false)
  })
})

describe('reviewer app.js dataset switching', () => {
  test('switching datasets resets the per-dataset ordered-id cache', async () => {
    const h = harness(new Map())
    const aIds = orderIds('A_CANONICAL_240', 2)
    const bIds = orderIds('B_SAFETY_99', 3)
    const routes = baseRoutes(h)
    routes.set('/api/order', () => {
      const last = h.fetchLog.filter((f) => f.path.includes('/api/order')).pop()

      const ds = new URL(last ? last.path : '', 'http://x')
        .searchParams.get('dataset')

      return {
        dataset_id: ds,
        sample_ids: ds === 'A_CANONICAL_240' ? aIds : bIds,
      }
    })
    routes.set('/api/resume', () => {
      const last = h.fetchLog.filter((f) => f.path.includes('/api/resume')).pop()

      const ds = new URL(last ? last.path : '', 'http://x')
        .searchParams.get('dataset')

      return {
        sample_id: ds === 'A_CANONICAL_240' ? aIds[0] : bIds[0],
      }
    })
    routes.set('/api/sample', () => {
      const last = h.fetchLog.filter((f) => f.path.includes('/api/sample')).pop()

      const sid = new URL(last ? last.path : '', 'http://x')
        .searchParams.get('sample_id')

      return samplePayload(
        sid ? sid.split('-').slice(0, -1).join('-') : 'A_CANONICAL_240',
        sid ?? '', ''
      )
    })
    h.route((url) => {
      for (const [needle, body] of routes) {
        if (url.includes(needle)) {
          return {
            ok: true, status: 200,
            headers: { get: (hdr: string) =>
              hdr.toLowerCase() === 'content-type' ? 'application/json' : null },
            json: async () => (typeof body === 'function' ? body() : body),
          }
        }
      }

      return {
        ok: false, status: 500,
        headers: { get: () => 'application/json' },
        json: async () => ({ error: 'unrouted ' + url }),
      }
    })
    boot(h)
    // startApp loads dataset A.
    await call(h, 'startApp')
    assert.deepEqual(getState(h, '_orderedIds'), aIds)
    // Switch to dataset B: the cache must be refetched, never reused.
    await call(h, 'selectDataset', 'B_SAFETY_99')
    assert.deepEqual(getState(h, '_orderedIds'), bIds)
    // Navigation inside B must only ever request B ids.
    await call(h, 'loadSampleAt', 2)
    const sampleCalls = h.fetchLog.filter((f) => f.path.includes('/api/sample'))
    const lastSample = sampleCalls[sampleCalls.length - 1]
    assert.ok(lastSample.path.includes('dataset=B_SAFETY_99'))
    assert.ok(lastSample.path.includes(`sample_id=${bIds[2]}`))
    // Back to A: ids return to A's order (never B's).
    await call(h, 'selectDataset', 'A_CANONICAL_240')
    assert.deepEqual(getState(h, '_orderedIds'), aIds)
  })
})
