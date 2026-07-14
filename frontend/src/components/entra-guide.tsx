import { Check, Copy, ExternalLink } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { Badge } from './ui/badge'
import { Button } from './ui/button'

const PERMISSIONS = [
  { name: 'User.Read.All', why: 'Benutzer, UPN, letzte Passwortänderung lesen', optional: false },
  {
    name: 'Domain.Read.All',
    why: 'Passwort-Gültigkeitsdauer der Domain ermitteln',
    optional: false,
  },
  { name: 'Mail.Send', why: 'Erinnerungs-E-Mails über Graph versenden', optional: false },
  {
    name: 'GroupMember.Read.All',
    why: 'Nur bei Sync-Gruppe & SSO — Gruppenmitglieder lesen',
    optional: true,
  },
]

const STEPS = [
  'Öffnen Sie das Entra-Admin-Center → „App-Registrierungen" → „Neue Registrierung".',
  'Vergeben Sie einen Namen (z. B. „PwNotify") und registrieren Sie die App (nur dieser Tenant).',
  'Kopieren Sie auf der Übersicht die „Anwendungs-(Client-)ID" und die „Verzeichnis-(Mandanten-)ID".',
  'Unter „Zertifikate & Geheimnisse" → „Neuer geheimer Clientschlüssel" erstellen und den Wert sofort kopieren.',
  'Unter „API-Berechtigungen" die Application-Permissions hinzufügen (Microsoft Graph → Anwendungsberechtigungen). GroupMember.Read.All nur, wenn Sie eine Sync-Gruppe oder SSO nutzen.',
  'Auf „Administratorzustimmung erteilen" klicken — die Status-Spalte muss grün sein.',
]

export function EntraGuide() {
  const [copied, setCopied] = useState<string | null>(null)

  const copy = (text: string) => {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(text)
      toast.success('Kopiert')
      setTimeout(() => setCopied(null), 1500)
    })
  }

  return (
    <div className="border-border bg-muted/40 rounded-lg border p-4">
      <div className="flex items-center justify-between">
        <h4 className="font-display text-sm font-semibold">
          Entra-App-Registrierung — Schritt für Schritt
        </h4>
        <Button variant="outline" size="sm" asChild>
          <a
            href="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
            target="_blank"
            rel="noreferrer"
          >
            Entra öffnen <ExternalLink className="size-3.5" />
          </a>
        </Button>
      </div>

      <ol className="mt-3 space-y-2">
        {STEPS.map((step, i) => (
          <li key={i} className="text-muted-foreground flex gap-3 text-sm">
            <span className="bg-primary/15 text-primary grid size-5 shrink-0 place-items-center rounded-full text-[11px] font-semibold">
              {i + 1}
            </span>
            <span>{step}</span>
          </li>
        ))}
      </ol>

      <div className="mt-4">
        <p className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
          Benötigte Application-Berechtigungen
        </p>
        <div className="space-y-1.5">
          {PERMISSIONS.map((p) => (
            <div
              key={p.name}
              className="border-border bg-card flex items-center justify-between gap-3 rounded-md border px-3 py-2"
            >
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <code className="font-mono text-xs font-semibold">{p.name}</code>
                  {p.optional && (
                    <Badge variant="outline" className="shrink-0">
                      Optional
                    </Badge>
                  )}
                </div>
                <p className="text-muted-foreground truncate text-xs">{p.why}</p>
              </div>
              <button
                type="button"
                onClick={() => copy(p.name)}
                className="text-muted-foreground hover:text-foreground shrink-0 rounded p-1"
                aria-label={`${p.name} kopieren`}
              >
                {copied === p.name ? (
                  <Check className="text-success size-3.5" />
                ) : (
                  <Copy className="size-3.5" />
                )}
              </button>
            </div>
          ))}
        </div>
        <div className="text-muted-foreground mt-3 flex items-start gap-2 text-xs">
          <Badge variant="outline">Wichtig</Badge>
          <span>
            Es müssen <strong>Anwendungsberechtigungen</strong> (nicht „Delegiert") sein, und die
            <strong> Administratorzustimmung</strong> muss erteilt werden — sonst schlägt der Sync
            fehl.
          </span>
        </div>
      </div>
    </div>
  )
}
