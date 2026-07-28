import { motion } from 'framer-motion'

/** Shared page-enter transition — a small upward fade, used for every routed
 * page so navigating through the app reads as one consistent motion
 * language rather than ad hoc per-page animation. */
export function PageTransition({ children }: { children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -8 }}
      transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  )
}
