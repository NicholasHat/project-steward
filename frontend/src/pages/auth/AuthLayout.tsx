import { motion } from 'framer-motion'
import { Logo } from '@/components/layout/Logo'

export function AuthLayout({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-zinc-50 px-4">
      {/* Ambient backdrop: a restrained radial wash in the accent, never used
          for chrome elsewhere — this is the one deliberately decorative use
          of the accent color, reserved for the brand moment. */}
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(60% 50% at 50% 0%, rgba(61,70,224,0.08) 0%, rgba(61,70,224,0) 70%)',
        }}
      />
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 w-full max-w-sm"
      >
        <div className="mb-8 flex flex-col items-center text-center">
          <Logo className="mb-6" />
          <h1 className="text-xl font-semibold tracking-tight text-zinc-900">{title}</h1>
          <p className="mt-1.5 text-sm text-zinc-500">{subtitle}</p>
        </div>
        <div className="rounded-xl border border-zinc-200 bg-white p-6 shadow-[var(--shadow-card)]">
          {children}
        </div>
      </motion.div>
    </div>
  )
}
