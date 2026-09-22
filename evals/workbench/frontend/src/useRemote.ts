import { useEffect, useState } from 'react'

export function useRemote<T>(load: () => Promise<T>) {
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState<{ load: () => Promise<T>; revision: number; data?: T; error: string }>({ load, revision, error: '' })
  useEffect(() => {
    let active = true
    load().then(data => { if (active) setResult({ load, revision, data, error: '' }) })
      .catch((e: Error) => { if (active) setResult({ load, revision, error: e.message }) })
    return () => { active = false }
  }, [load, revision])
  const current = result.load === load && result.revision === revision
  return { data: current ? result.data : undefined, error: current ? result.error : '', reload: () => setRevision(r => r + 1) }
}
