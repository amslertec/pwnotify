import { useQuery } from '@tanstack/react-query'
import { ArrowUpCircle } from 'lucide-react'
import { useState } from 'react'

import { Button } from './ui/button'
import { api } from '@/lib/api'
import type { VersionInfo } from '@/lib/types'

const ACK_KEY = 'pwnotify-update-ack'

/** Modal, das erscheint, sobald ein neueres GitHub-Release als die laufende Version
 *  existiert. Muss aktiv bestätigt werden (kein Wegklicken) und zeigt die Release-Notes.
 *  Pro Version einmal bestätigbar; bei noch neuerer Version erscheint es erneut. */
export function UpdateModal() {
  const { data } = useQuery({
    queryKey: ['version'],
    queryFn: () => api.get<VersionInfo>('/version'),
    staleTime: 60 * 60 * 1000,
    // Auch lange offene Sessions prüfen periodisch nach (Backend cacht 6 h -> günstig).
    refetchInterval: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  })
  const [ack, setAck] = useState(() => localStorage.getItem(ACK_KEY))

  if (!data?.update_available || !data.latest) return null
  if (ack === data.latest) return null

  const confirm = () => {
    localStorage.setItem(ACK_KEY, data.latest as string)
    setAck(data.latest)
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="update-modal-title"
    >
      <div className="border-border bg-card w-full max-w-lg rounded-xl border p-6 shadow-xl">
        <div className="flex items-center gap-3">
          <div className="bg-primary/15 text-primary grid size-10 shrink-0 place-items-center rounded-lg">
            <ArrowUpCircle className="size-5" />
          </div>
          <div className="min-w-0">
            <h2 id="update-modal-title" className="font-display text-lg font-semibold">
              Update verfügbar
            </h2>
            <p className="text-muted-foreground text-sm">
              Neue Version <strong className="text-foreground">{data.latest}</strong> · installiert:{' '}
              {data.current}
            </p>
          </div>
        </div>

        <div className="mt-4">
          <p className="mb-2 text-sm font-medium">
            {data.release_name || 'Änderungen in diesem Release'}
          </p>
          <div className="border-border bg-muted/30 max-h-72 overflow-y-auto rounded-lg border p-4">
            {data.notes ? (
              <pre className="text-foreground font-sans text-sm break-words whitespace-pre-wrap">
                {data.notes}
              </pre>
            ) : (
              <p className="text-muted-foreground text-sm">
                Keine Detailinformationen zum Release verfügbar.
              </p>
            )}
          </div>
          <a
            href={data.release_url}
            target="_blank"
            rel="noreferrer"
            className="text-primary mt-2 inline-block text-sm font-medium underline underline-offset-2"
          >
            Vollständige Release-Details auf GitHub
          </a>
        </div>

        <div className="mt-6 flex justify-end">
          <Button onClick={confirm}>Gelesen &amp; verstanden</Button>
        </div>
      </div>
    </div>
  )
}
