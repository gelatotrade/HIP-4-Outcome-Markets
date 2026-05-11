//! Persistent monotonic nonce.
//!
//! Hyperliquid /exchange requires `nonce > previous-nonce-for-this-account`.
//! After a restart the process must continue past the last value it
//! submitted, otherwise the API rejects the request and the order is
//! silently dropped.
//!
//! Storage: a tiny file with the most recent nonce as 8 big-endian bytes.
//! Written via tempfile + atomic `rename` so a crash never leaves a
//! half-written value.
//!
//! On boot the file is read; `next()` returns `max(persisted+1, now_ms)`
//! and persists the new value before handing it out. Persisting BEFORE
//! handing out is conservative: it costs us at most one "wasted" nonce
//! on crash, but we never re-use one.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use anyhow::{Context, Result};

pub struct NonceStore {
    path: PathBuf,
    inner: Mutex<u64>,
}

impl NonceStore {
    /// Open a NonceStore at `path`. Creates the parent dir and an
    /// initial file seeded to current wall-clock ms if the file doesn't
    /// exist.
    pub fn open(path: impl Into<PathBuf>) -> Result<Self> {
        let path = path.into();
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("create dir {}", parent.display()))?;
        }
        let initial = if path.exists() {
            read_file(&path)?
        } else {
            now_ms()
        };
        let store = Self {
            path,
            inner: Mutex::new(initial),
        };
        store.persist(initial)?;
        Ok(store)
    }

    /// Issue the next nonce. Always strictly greater than any previously
    /// issued nonce for this store, and at least `now_ms()`.
    pub fn next(&self) -> Result<u64> {
        let mut guard = self.inner.lock().expect("nonce mutex poisoned");
        let candidate = (*guard + 1).max(now_ms());
        self.persist(candidate)?;
        *guard = candidate;
        Ok(candidate)
    }

    /// Peek without advancing — for diagnostics only.
    pub fn current(&self) -> u64 {
        *self.inner.lock().expect("nonce mutex poisoned")
    }

    fn persist(&self, value: u64) -> Result<()> {
        let tmp = self.path.with_extension("tmp");
        std::fs::write(&tmp, value.to_be_bytes())
            .with_context(|| format!("write {}", tmp.display()))?;
        std::fs::rename(&tmp, &self.path)
            .with_context(|| format!("rename {} -> {}", tmp.display(), self.path.display()))?;
        Ok(())
    }
}

fn read_file(path: &Path) -> Result<u64> {
    let bytes = std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
    if bytes.len() != 8 {
        return Ok(now_ms());
    }
    let mut buf = [0u8; 8];
    buf.copy_from_slice(&bytes);
    Ok(u64::from_be_bytes(buf))
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn fresh_store_initialises_from_clock() {
        let dir = tempdir().unwrap();
        let store = NonceStore::open(dir.path().join("nonce.bin")).unwrap();
        let a = store.next().unwrap();
        let b = store.next().unwrap();
        assert!(b > a, "{} not > {}", b, a);
    }

    #[test]
    fn reopen_resumes_past_last_issued() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("nonce.bin");
        let issued;
        {
            let store = NonceStore::open(&path).unwrap();
            for _ in 0..3 {
                let _ = store.next().unwrap();
            }
            issued = store.current();
        }
        let reopened = NonceStore::open(&path).unwrap();
        let next = reopened.next().unwrap();
        assert!(next > issued, "{} not > {}", next, issued);
    }

    #[test]
    fn monotonic_across_threads() {
        use std::sync::Arc;
        use std::thread;

        let dir = tempdir().unwrap();
        let store = Arc::new(NonceStore::open(dir.path().join("nonce.bin")).unwrap());
        let mut handles = Vec::new();
        for _ in 0..8 {
            let store = store.clone();
            handles.push(thread::spawn(move || {
                let mut out = Vec::new();
                for _ in 0..50 {
                    out.push(store.next().unwrap());
                }
                out
            }));
        }
        let mut all: Vec<u64> = handles
            .into_iter()
            .flat_map(|h| h.join().unwrap())
            .collect();
        all.sort_unstable();
        for w in all.windows(2) {
            assert!(w[1] > w[0], "duplicate nonce {} and {}", w[0], w[1]);
        }
    }
}
