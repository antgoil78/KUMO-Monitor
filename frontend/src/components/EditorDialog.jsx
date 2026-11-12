import { useEffect, useRef } from 'react'

export default function EditorDialog({ title, onClose, busy = false, children }) {
  const ref = useRef(null)
  const closeRef = useRef(onClose)
  const busyRef = useRef(busy)
  closeRef.current = onClose
  busyRef.current = busy
  useEffect(() => {
    const previous = document.activeElement
    const element = ref.current
    const focusable = () => [...element.querySelectorAll('button, input, select, textarea, [tabindex="0"]')].filter(item => !item.disabled)
    ;(element.querySelector('input, select, textarea') || focusable()[0])?.focus()
    function handle(event) {
      if (event.key === 'Escape' && !busyRef.current) closeRef.current()
      if (event.key === 'Tab') {
        const items = focusable(), first = items[0], last = items.at(-1)
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    element.addEventListener('keydown', handle)
    return () => { element.removeEventListener('keydown', handle); previous?.focus() }
  }, [])
  return <div ref={ref} className="modal-backdrop" role="dialog" aria-modal="true" aria-label={title} onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose() }}>{children}</div>
}
