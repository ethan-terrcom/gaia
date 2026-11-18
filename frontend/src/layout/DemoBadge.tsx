import { useQuery } from '@tanstack/react-query'
import { fetchMe } from '../api/client'

/**
 * An amber badge shown while ``GAIA_FAKE_LLM=1`` demo mode is active. Reuses
 * the shell's ``['me']`` query key, so ``GET /me`` is fetched once.
 */
export function DemoBadge() {
  const { data } = useQuery({ queryKey: ['me'], queryFn: fetchMe })

  if (!data?.demo_mode) {
    return null
  }

  return (
    <span
      title="GAIA_FAKE_LLM=1: no real model is called"
      className="rounded-full bg-[var(--color-warning)] px-2 py-0.5 text-[11px] font-medium tracking-[-0.01em] text-black"
    >
      Demo
    </span>
  )
}
