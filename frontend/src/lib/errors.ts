import i18n from '@/i18n'
import { ApiError } from './api'

/** Übersetzt einen Fehler für die Anzeige: erst über den Backend-Fehlercode
 *  (`errors.<code>`), sonst Fallback auf den Servertext bzw. eine generische Meldung.
 *  `detail` (der Servertext) steht der Übersetzung als Interpolation zur Verfügung. */
export function translateError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code) {
      const translated = i18n.t(`errors.${err.code}`, {
        detail: err.message,
        defaultValue: '',
      })
      if (translated) return translated
    }
    return err.message || i18n.t('errors.generic')
  }
  return i18n.t('errors.generic')
}
