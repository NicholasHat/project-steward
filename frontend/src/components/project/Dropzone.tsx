import * as React from 'react'
import { motion } from 'framer-motion'
import { File, UploadCloud, X } from 'lucide-react'
import { cn, formatBytes } from '@/lib/utils'
import { Button } from '@/components/ui/button'

interface DropzoneProps {
  files: File[]
  onFilesChange: (files: File[]) => void
  disabled?: boolean
}

/** Native HTML5 drag-and-drop staging area — no extra dependency, since the
 * only behavior needed is "collect File objects before POST .../files". */
export function Dropzone({ files, onFilesChange, disabled }: DropzoneProps) {
  const [isDragging, setIsDragging] = React.useState(false)
  const inputRef = React.useRef<HTMLInputElement>(null)

  function addFiles(list: FileList | null) {
    if (!list) return
    const incoming = Array.from(list)
    const existingKeys = new Set(files.map((f) => `${f.name}:${f.size}`))
    const merged = [...files, ...incoming.filter((f) => !existingKeys.has(`${f.name}:${f.size}`))]
    onFilesChange(merged)
  }

  return (
    <div>
      <div
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled) setIsDragging(true)
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setIsDragging(false)
          if (!disabled) addFiles(e.dataTransfer.files)
        }}
        onClick={() => !disabled && inputRef.current?.click()}
        className={cn(
          'flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-14 text-center transition-colors',
          isDragging
            ? 'border-signal-400 bg-signal-50'
            : 'border-zinc-300 bg-zinc-50/60 hover:border-zinc-400 hover:bg-zinc-50',
          disabled && 'pointer-events-none opacity-50',
        )}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => addFiles(e.target.files)}
        />
        <motion.div
          animate={isDragging ? { scale: 1.08, y: -2 } : { scale: 1, y: 0 }}
          transition={{ duration: 0.15 }}
          className="mb-3 flex size-12 items-center justify-center rounded-full bg-white text-signal-600 shadow-[var(--shadow-card)]"
        >
          <UploadCloud className="size-5" />
        </motion.div>
        <p className="text-sm font-medium text-zinc-700">
          Drag files here, or <span className="text-signal-600">browse</span>
        </p>
        <p className="mt-1 text-xs text-zinc-400">
          PDF, DOCX, XLSX/CSV, PPTX, images, and plain text/markdown
        </p>
      </div>

      {files.length > 0 && (
        <ul className="mt-4 divide-y divide-zinc-100 rounded-lg border border-zinc-200 bg-white">
          {files.map((file, i) => (
            <motion.li
              key={`${file.name}:${file.size}`}
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.2, delay: Math.min(i, 8) * 0.02 }}
              className="flex items-center justify-between gap-3 px-3 py-2"
            >
              <div className="flex min-w-0 items-center gap-2.5">
                <File className="size-4 shrink-0 text-zinc-400" />
                <span className="truncate text-sm text-zinc-700">{file.name}</span>
                <span className="shrink-0 font-mono text-xs text-zinc-400">{formatBytes(file.size)}</span>
              </div>
              {!disabled && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-6 shrink-0"
                  onClick={() => onFilesChange(files.filter((_, idx) => idx !== i))}
                >
                  <X className="size-3.5" />
                </Button>
              )}
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  )
}
