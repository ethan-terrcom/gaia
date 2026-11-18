import { describe, expect, it } from 'vitest'
import { advisories, evaluate } from './audit-check.mjs'

const report = {
  vulnerabilities: {
    picomatch: {
      severity: 'high',
      via: [
        {
          name: 'picomatch',
          severity: 'high',
          title: 'ReDoS',
          url: 'https://github.com/advisories/GHSA-aaaa',
        },
      ],
    },
    vite: { severity: 'high', via: ['picomatch'] },
    lodash: {
      severity: 'moderate',
      via: [
        {
          severity: 'moderate',
          title: 'Prototype pollution',
          url: 'https://github.com/advisories/GHSA-bbbb',
        },
      ],
    },
    other: {
      severity: 'critical',
      via: [
        {
          name: 'other',
          severity: 'critical',
          title: 'RCE',
          url: 'https://github.com/advisories/GHSA-cccc',
        },
        {
          name: 'other',
          severity: 'critical',
          title: 'RCE',
          url: 'https://github.com/advisories/GHSA-cccc',
        },
      ],
    },
  },
}

describe('audit-check', () => {
  it('reads each advisory once and ignores propagation entries', () => {
    expect(advisories(report).map((advisory) => advisory.id)).toEqual([
      'GHSA-aaaa',
      'GHSA-bbbb',
      'GHSA-cccc',
    ])
  })

  it('blocks high and critical advisories that have no exception', () => {
    const result = evaluate(report, [], '2026-11-17')

    expect(result.blocking.map((advisory) => advisory.id)).toEqual([
      'GHSA-aaaa',
      'GHSA-cccc',
    ])
    expect(result.allowed).toEqual([])
  })

  it('allows an excepted advisory until its expiry date, inclusive', () => {
    const allowlist = [{ id: 'GHSA-aaaa', expires: '2026-12-17' }]

    const before = evaluate(report, allowlist, '2026-12-17')
    const after = evaluate(report, allowlist, '2026-12-18')

    expect(before.allowed.map((advisory) => advisory.id)).toEqual(['GHSA-aaaa'])
    expect(before.blocking.map((advisory) => advisory.id)).toEqual([
      'GHSA-cccc',
    ])
    expect(after.expired.map((advisory) => advisory.id)).toEqual(['GHSA-aaaa'])
  })

  it('treats an empty report as clean', () => {
    expect(evaluate({}, [], '2026-11-17')).toEqual({
      blocking: [],
      allowed: [],
      expired: [],
    })
  })
})
