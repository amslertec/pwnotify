import { Download, MoreVertical, Plus, Share, Smartphone, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Button } from './ui/button'

const DISMISS_KEY = 'pwnotify-pwa-dismissed'

type BeforeInstallPromptEvent = Event & {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

/** Läuft die App bereits als installierte PWA? */
function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as unknown as { standalone?: boolean }).standalone === true
  )
}

function detectPlatform(): 'ios' | 'android' | 'other' {
  const ua = navigator.userAgent
  // iPadOS meldet sich als "MacIntel" mit Touch — mit abfangen.
  if (/iphone|ipad|ipod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1))
    return 'ios'
  if (/android/i.test(ua)) return 'android'
  return 'other'
}

/** Install-Hinweis: erscheint nur auf Mobilgeräten, nicht wenn bereits installiert
 *  oder zuvor ausgeblendet. Bietet den nativen Prompt (Android/Chrome) bzw. eine
 *  plattformspezifische Schritt-für-Schritt-Anleitung (iOS/Safari). */
export function InstallPrompt() {
  const { t } = useTranslation()
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null)
  const [visible, setVisible] = useState(false)
  const platform = detectPlatform()

  useEffect(() => {
    if (platform === 'other') return // nur Mobilgeräte
    if (isStandalone()) return // schon installiert -> nie zeigen
    if (localStorage.getItem(DISMISS_KEY)) return // zuvor ausgeblendet

    const onBip = (e: Event) => {
      e.preventDefault()
      setDeferred(e as BeforeInstallPromptEvent)
    }
    const onInstalled = () => {
      localStorage.setItem(DISMISS_KEY, '1')
      setVisible(false)
    }
    window.addEventListener('beforeinstallprompt', onBip)
    window.addEventListener('appinstalled', onInstalled)
    setVisible(true)
    return () => {
      window.removeEventListener('beforeinstallprompt', onBip)
      window.removeEventListener('appinstalled', onInstalled)
    }
  }, [platform])

  if (!visible) return null

  const dismiss = () => {
    localStorage.setItem(DISMISS_KEY, '1')
    setVisible(false)
  }

  const installNative = async () => {
    if (!deferred) return
    await deferred.prompt()
    await deferred.userChoice
    dismiss()
  }

  const steps: { icon: typeof Share; text: string }[] =
    platform === 'ios'
      ? [
          { icon: Share, text: t('pwa.ios.step1') },
          { icon: Plus, text: t('pwa.ios.step2') },
        ]
      : [
          { icon: MoreVertical, text: t('pwa.android.step1') },
          { icon: Plus, text: t('pwa.android.step2') },
        ]

  return (
    <div className="fixed inset-x-0 bottom-0 z-[90] p-3">
      <div className="border-border bg-card mx-auto max-w-md rounded-2xl border p-4 shadow-xl">
        <div className="flex items-start gap-3">
          <div className="bg-primary/15 text-primary grid size-10 shrink-0 place-items-center rounded-xl">
            <Smartphone className="size-5" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold">{t('pwa.title')}</p>
            <p className="text-muted-foreground text-xs">{t('pwa.subtitle')}</p>
          </div>
          <button
            onClick={dismiss}
            className="text-muted-foreground hover:text-foreground -mt-1 -mr-1 shrink-0 rounded p-1"
            aria-label={t('pwa.dismiss')}
          >
            <X className="size-4" />
          </button>
        </div>

        {deferred ? (
          <Button className="mt-3 w-full" onClick={installNative}>
            <Download className="size-4" /> {t('pwa.installButton')}
          </Button>
        ) : (
          <div className="mt-3">
            <p className="text-muted-foreground mb-2 text-xs font-medium">
              {platform === 'ios' ? t('pwa.ios.title') : t('pwa.android.title')}
            </p>
            <ol className="space-y-2">
              {steps.map((s, i) => (
                <li key={i} className="flex items-center gap-2.5 text-sm">
                  <span className="bg-muted grid size-6 shrink-0 place-items-center rounded-md">
                    <s.icon className="size-3.5" />
                  </span>
                  <span>{s.text}</span>
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </div>
  )
}
