#!/usr/bin/env node
// `npm audit` for `make audit` (doc 18c): fail on any high or critical advisory,
// except the dated exceptions in allowlist.json. Each exception is an accepted
// risk recorded in docs/SECURITY.md, and it stops counting on its `expires`
// date, so a forgotten exception turns the audit red again instead of hiding
// the package for good.
import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const BLOCKING_SEVERITIES = new Set(['high', 'critical'])

/** The advisories in an `npm audit --json` report, one per advisory id. */
export function advisories(report) {
  const found = new Map()
  for (const [name, vulnerability] of Object.entries(
    report.vulnerabilities ?? {},
  )) {
    for (const via of vulnerability.via ?? []) {
      // A string `via` names another vulnerable package: propagation, not a
      // finding of its own. The advisory itself is the object form.
      if (typeof via !== 'object' || via === null || !via.url) {
        continue
      }
      const id = via.url.split('/').pop()
      if (!found.has(id)) {
        found.set(id, {
          id,
          package: via.name ?? name,
          severity: via.severity ?? vulnerability.severity,
          title: via.title ?? '',
          url: via.url,
        })
      }
    }
  }
  return [...found.values()]
}

/**
 * Split a report's high and critical advisories into blocking and allowed.
 *
 * `today` is an ISO date (`YYYY-MM-DD`). An exception counts while
 * `today <= expires`; after that its advisory blocks again, listed as expired.
 */
export function evaluate(report, allowlist, today) {
  const exceptions = new Map(allowlist.map((entry) => [entry.id, entry]))
  const result = { blocking: [], allowed: [], expired: [] }
  for (const advisory of advisories(report)) {
    if (!BLOCKING_SEVERITIES.has(advisory.severity)) {
      continue
    }
    const exception = exceptions.get(advisory.id)
    if (exception === undefined) {
      result.blocking.push(advisory)
    } else if (today > exception.expires) {
      result.expired.push({ ...advisory, expires: exception.expires })
    } else {
      result.allowed.push({ ...advisory, expires: exception.expires })
    }
  }
  return result
}

function runNpmAudit(cwd) {
  try {
    return execFileSync('npm', ['audit', '--json'], {
      cwd,
      encoding: 'utf8',
      maxBuffer: 64 * 1024 * 1024,
    })
  } catch (error) {
    // `npm audit` exits non-zero whenever it finds anything; the report is
    // still on stdout. No stdout means npm itself failed.
    if (error.stdout) {
      return error.stdout
    }
    throw error
  }
}

function main() {
  const allowlistPath = fileURLToPath(
    new URL('./allowlist.json', import.meta.url),
  )
  const allowlist = JSON.parse(readFileSync(allowlistPath, 'utf8'))
  const report = JSON.parse(runNpmAudit(process.cwd()))
  const today = new Date().toISOString().slice(0, 10)
  const { blocking, allowed, expired } = evaluate(report, allowlist, today)

  for (const advisory of allowed) {
    console.warn(
      `accepted until ${advisory.expires}: ${advisory.id} ${advisory.package} (${advisory.severity}) ${advisory.title}`,
    )
  }
  for (const advisory of expired) {
    console.error(
      `exception expired ${advisory.expires}, re-check: ${advisory.id} ${advisory.package} ${advisory.url}`,
    )
  }
  for (const advisory of blocking) {
    console.error(
      `${advisory.severity}: ${advisory.id} ${advisory.package} ${advisory.title} ${advisory.url}`,
    )
  }
  const failures = blocking.length + expired.length
  console.log(
    failures === 0
      ? `npm audit: no blocking advisories (${allowed.length} accepted)`
      : `npm audit: ${failures} blocking advisories`,
  )
  process.exitCode = failures === 0 ? 0 : 1
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main()
}
