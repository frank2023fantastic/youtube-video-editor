import { useState, useRef, useCallback } from 'react'
import './App.css'

const LANGUAGES = [
  { value: 'spanish', label: '🇪🇸 Spanish' },
  { value: 'french', label: '🇫🇷 French' },
  { value: 'german', label: '🇩🇪 German' },
  { value: 'japanese', label: '🇯🇵 Japanese' },
  { value: 'chinese', label: '🇨🇳 Chinese' },
  { value: 'korean', label: '🇰🇷 Korean' },
  { value: 'portuguese', label: '🇧🇷 Portuguese' },
  { value: 'italian', label: '🇮🇹 Italian' },
  { value: 'arabic', label: '🇸🇦 Arabic' },
  { value: 'hindi', label: '🇮🇳 Hindi' },
  { value: 'russian', label: '🇷🇺 Russian' },
  { value: 'turkish', label: '🇹🇷 Turkish' },
]

const PIPELINE_STEPS = [
  { key: 'ingesting', label: 'Ingest', icon: '📥' },
  { key: 'separating', label: 'Separate', icon: '🎵' },
  { key: 'transcribing', label: 'Transcribe', icon: '📝' },
  { key: 'translating', label: 'Translate', icon: '🌍' },
  { key: 'synthesizing', label: 'Synthesize', icon: '🗣️' },
  { key: 'mixing', label: 'Mix & Render', icon: '🎬' },
]

function App() {
  const [file, setFile] = useState(null)
  const [language, setLanguage] = useState('spanish')
  const [jobId, setJobId] = useState(null)
  const [status, setStatus] = useState(null)
  const [isProcessing, setIsProcessing] = useState(false)
  const [isDragging, setIsDragging] = useState(false)
  const fileInputRef = useRef(null)
  const eventSourceRef = useRef(null)

  const handleFileSelect = (selectedFile) => {
    if (selectedFile && selectedFile.type.startsWith('video/')) {
      setFile(selectedFile)
    }
  }

  const handleDrop = (e) => {
    e.preventDefault()
    setIsDragging(false)
    const droppedFile = e.dataTransfer.files[0]
    handleFileSelect(droppedFile)
  }

  const handleDragOver = (e) => {
    e.preventDefault()
    setIsDragging(true)
  }

  const handleDragLeave = () => {
    setIsDragging(false)
  }

  const startProcessing = useCallback(async () => {
    if (!file) return

    setIsProcessing(true)
    setStatus(null)

    try {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('target_language', language)

      const res = await fetch('/api/process', {
        method: 'POST',
        body: formData,
      })

      if (!res.ok) {
        let errMsg = `Request failed (${res.status})`
        try {
          const text = await res.text()
          if (text) {
            const err = JSON.parse(text)
            errMsg = err.detail || errMsg
          }
        } catch { /* non-JSON response */ }
        setStatus({ status: 'failed', message: errMsg, progress: 0 })
        setIsProcessing(false)
        return
      }

      let data
      try {
        data = await res.json()
      } catch {
        setStatus({ status: 'failed', message: 'Invalid response from server', progress: 0 })
        setIsProcessing(false)
        return
      }
      setJobId(data.job_id)

      // Start SSE listener
      if (eventSourceRef.current) eventSourceRef.current.close()

      const evtSource = new EventSource(`/api/status/${data.job_id}`)
      eventSourceRef.current = evtSource

      evtSource.onmessage = (event) => {
        try {
          const update = JSON.parse(event.data)
          setStatus(update)

          if (update.status === 'completed' || update.status === 'failed') {
            evtSource.close()
            setIsProcessing(false)
          }
        } catch (e) {
          console.error('SSE parse error:', e)
        }
      }

      evtSource.onerror = () => {
        evtSource.close()
        setIsProcessing(false)
      }
    } catch (err) {
      setStatus({ status: 'failed', message: `Network error: ${err.message}`, progress: 0 })
      setIsProcessing(false)
    }
  }, [file, language])

  const getStepStatus = (stepKey) => {
    if (!status) return 'pending'
    const stepIndex = PIPELINE_STEPS.findIndex(s => s.key === stepKey)
    const currentIndex = PIPELINE_STEPS.findIndex(s => s.key === status.step)

    if (status.status === 'failed') {
      if (stepIndex <= currentIndex) return stepIndex === currentIndex ? 'failed' : 'completed'
      return 'pending'
    }
    if (stepIndex < currentIndex) return 'completed'
    if (stepIndex === currentIndex) return 'active'
    return 'pending'
  }

  const formatFileSize = (bytes) => {
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  return (
    <div className="bg-gradient-animated min-h-screen relative">
      {/* Floating particles */}
      <div className="particle" style={{ width: 300, height: 300, top: '10%', left: '-5%', background: 'radial-gradient(circle, rgba(108,92,231,0.15), transparent 70%)' }} />
      <div className="particle" style={{ width: 400, height: 400, bottom: '5%', right: '-8%', background: 'radial-gradient(circle, rgba(168,85,247,0.1), transparent 70%)', animationDelay: '5s' }} />
      <div className="particle" style={{ width: 200, height: 200, top: '50%', left: '60%', background: 'radial-gradient(circle, rgba(0,206,201,0.08), transparent 70%)', animationDelay: '10s' }} />

      <div className="max-w-2xl mx-auto px-4 py-12 relative z-10">
        {/* Header */}
        <header className="text-center mb-12 fade-in">
          <div className="inline-flex items-center gap-3 mb-4">
            <span className="text-4xl">🎬</span>
            <h1 className="text-4xl font-bold tracking-tight logo-glow">
              <span className="bg-gradient-to-r from-[var(--color-accent-secondary)] to-[var(--color-success)] bg-clip-text text-transparent">
                VoiceDub
              </span>
            </h1>
          </div>
          <p className="text-[var(--color-text-secondary)] text-lg">
            AI-powered multi-language video dubbing
          </p>
        </header>

        {/* Main card */}
        <div className="glass-card p-8 fade-in" style={{ animationDelay: '0.1s' }}>
          <div className="space-y-6">
            {/* File Upload Area */}
            <div>
              <label className="block text-sm font-medium text-[var(--color-text-secondary)] mb-2">
                Upload Video
              </label>
              <div
                className={`upload-zone ${isDragging ? 'dragging' : ''} ${file ? 'has-file' : ''}`}
                onDrop={handleDrop}
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onClick={() => !isProcessing && fileInputRef.current?.click()}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="video/*"
                  className="hidden"
                  onChange={(e) => handleFileSelect(e.target.files[0])}
                  disabled={isProcessing}
                />
                {file ? (
                  <div className="text-center">
                    <span className="text-3xl block mb-2">🎥</span>
                    <p className="text-[var(--color-text-primary)] font-medium">{file.name}</p>
                    <p className="text-[var(--color-text-secondary)] text-sm mt-1">{formatFileSize(file.size)}</p>
                    {!isProcessing && (
                      <p className="text-[var(--color-accent-secondary)] text-xs mt-2 opacity-70">Click to change file</p>
                    )}
                  </div>
                ) : (
                  <div className="text-center">
                    <span className="text-4xl block mb-3">📁</span>
                    <p className="text-[var(--color-text-secondary)]">
                      Drag & drop your video here
                    </p>
                    <p className="text-[var(--color-text-secondary)] text-sm mt-1 opacity-60">
                      or click to browse — MP4, MKV, WebM, AVI, MOV
                    </p>
                  </div>
                )}
              </div>
            </div>

            {/* Language select */}
            <div>
              <label className="block text-sm font-medium text-[var(--color-text-secondary)] mb-2">
                Target Language
              </label>
              <select
                className="select-field"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                disabled={isProcessing}
              >
                {LANGUAGES.map(lang => (
                  <option key={lang.value} value={lang.value}>{lang.label}</option>
                ))}
              </select>
            </div>

            {/* Process button */}
            <button
              className="btn-primary w-full text-lg"
              onClick={startProcessing}
              disabled={isProcessing || !file}
            >
              {isProcessing ? (
                <span className="flex items-center justify-center gap-2">
                  <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  Processing…
                </span>
              ) : (
                '🚀 Start Dubbing'
              )}
            </button>
          </div>
        </div>

        {/* Progress section */}
        {status && (
          <div className="glass-card p-8 mt-6 fade-in">
            {/* Progress bar */}
            <div className="mb-6">
              <div className="flex justify-between items-center mb-2">
                <span className="text-sm font-semibold text-[var(--color-text-secondary)]">Progress</span>
                <span className="text-sm font-bold text-[var(--color-accent-secondary)]">{status.progress || 0}%</span>
              </div>
              <div className="progress-track">
                <div className="progress-fill" style={{ width: `${status.progress || 0}%` }} />
              </div>
            </div>

            {/* Pipeline steps */}
            <div className="grid grid-cols-3 gap-3 mb-6">
              {PIPELINE_STEPS.map(step => {
                const state = getStepStatus(step.key)
                return (
                  <div
                    key={step.key}
                    className={`step-badge justify-center ${state === 'active' ? 'active' : state === 'completed' ? 'completed' : state === 'failed' ? 'failed' : 'opacity-40'}`}
                  >
                    {state === 'active' && <div className="pulse-dot" />}
                    {state === 'completed' && <span>✓</span>}
                    {state === 'failed' && <span>✕</span>}
                    <span>{step.icon}</span>
                    <span className="hidden sm:inline">{step.label}</span>
                  </div>
                )
              })}
            </div>

            {/* Status message */}
            <div className={`text-sm text-center py-3 px-4 rounded-xl ${status.status === 'failed'
              ? 'bg-red-500/10 text-[var(--color-danger)] border border-red-500/20'
              : status.status === 'completed'
                ? 'bg-emerald-500/10 text-[var(--color-success)] border border-emerald-500/20'
                : 'bg-[var(--color-accent-primary)]/10 text-[var(--color-text-secondary)]'
              }`}>
              {status.message}
            </div>

            {/* Download button */}
            {status.status === 'completed' && jobId && (
              <div className="mt-6 text-center fade-in">
                <a
                  href={`/api/download/${jobId}`}
                  className="btn-download inline-flex items-center gap-2 text-lg"
                  download
                >
                  <span>⬇️</span> Download Dubbed Video
                </a>
              </div>
            )}

            {/* Error retry */}
            {status.status === 'failed' && (
              <div className="mt-4 text-center">
                <button
                  className="btn-primary"
                  onClick={() => { setStatus(null); setIsProcessing(false); setJobId(null); }}
                >
                  Try Again
                </button>
              </div>
            )}
          </div>
        )}

        {/* Footer */}
        <footer className="text-center mt-10 text-[var(--color-text-secondary)] text-xs opacity-60">
          <p>Powered by Whisper • Edge-TTS • Demucs • FFmpeg</p>
        </footer>
      </div>
    </div>
  )
}

export default App
