import { useBranding } from './branding-provider'
import { useTheme } from './theme-provider'
import { cn } from '@/lib/utils'

/** Marken-Logo: nutzt Upload falls vorhanden, sonst die Brand-SVGs (theme-abhängig).
 *  Custom-Logos werden mit object-contain in eine feste Box eingepasst (keine Verzerrung). */
export function Logo({
  collapsed = false,
  className,
}: {
  collapsed?: boolean
  className?: string
}) {
  const { resolved } = useTheme()
  const { branding } = useBranding()

  if (branding.has_logo && !collapsed) {
    // Exakt gleiche Grösse wie das Standard-Logo.
    return (
      <img
        src={`/api/branding/logo?v=${branding.logo_version}`}
        alt={branding.app_name}
        className={cn('h-8 w-auto', className)}
      />
    )
  }

  const dark = resolved === 'dark'
  const src = collapsed
    ? dark
      ? '/brand/icon-dark.svg'
      : '/brand/icon-light.svg'
    : dark
      ? '/brand/logo-dark.svg'
      : '/brand/logo-light.svg'

  return (
    <img
      src={src}
      alt={branding.app_name}
      className={cn(collapsed ? 'h-7 w-7' : 'h-8 w-auto', className)}
    />
  )
}
