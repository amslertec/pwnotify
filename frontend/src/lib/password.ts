/**
 * Gemeinsame Passwort-Policy für Frontend-UX (Setup, Invite-Accept, Reset).
 *
 * Diese Regeln MÜSSEN mit der serverseitigen Policy übereinstimmen (Backend
 * Task 5: mind. 10 Zeichen, Gross-/Kleinbuchstabe, Ziffer, Sonderzeichen).
 * Der Server bleibt die Autorität — dies ist nur die UI-Vorschau/UX.
 */
export interface PasswordRules {
  length: boolean
  upper: boolean
  lower: boolean
  digit: boolean
  special: boolean
}

export function checkPassword(pw: string): PasswordRules {
  return {
    length: pw.length >= 10,
    upper: /[A-Z]/.test(pw),
    lower: /[a-z]/.test(pw),
    digit: /[0-9]/.test(pw),
    special: /[^A-Za-z0-9]/.test(pw),
  }
}

export function passwordValid(pw: string): boolean {
  const rules = checkPassword(pw)
  return rules.length && rules.upper && rules.lower && rules.digit && rules.special
}

export function passwordsMatch(a: string, b: string): boolean {
  return a === b && a.length > 0
}
