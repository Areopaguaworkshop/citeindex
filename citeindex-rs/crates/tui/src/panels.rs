//! TUI panels — chat window, side panel, document tracker.
//!
//! Matches `frontend_ui_rust.yaml → layout_main`.

/// A single message in the chat window.
#[derive(Debug, Clone)]
pub struct ChatMessage {
    pub role: MessageRole,
    pub content: String,
    pub citations: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MessageRole {
    User,
    Assistant,
    System,
}

/// Chat window state — scrollable message history.
pub struct ChatWindow {
    pub messages: Vec<ChatMessage>,
    pub scroll_offset: u16,
}

impl ChatWindow {
    pub fn new() -> Self {
        Self {
            messages: vec![ChatMessage {
                role: MessageRole::System,
                content: "Welcome to CiteIndex. Type a message or / for commands.".into(),
                citations: Vec::new(),
            }],
            scroll_offset: 0,
        }
    }

    pub fn push(&mut self, msg: ChatMessage) {
        self.messages.push(msg);
        self.scroll_to_bottom();
    }

    pub fn scroll_up(&mut self, amount: u16) {
        self.scroll_offset = self.scroll_offset.saturating_add(amount);
    }

    pub fn scroll_down(&mut self, amount: u16) {
        self.scroll_offset = self.scroll_offset.saturating_sub(amount);
    }

    pub fn scroll_to_bottom(&mut self) {
        self.scroll_offset = 0;
    }

    pub fn clear(&mut self) {
        self.messages.clear();
        self.scroll_offset = 0;
    }
}

/// Side panel state — recent documents, search results, plugin shortcuts.
pub struct SidePanel {
    pub collapsed: bool,
    pub width_pct: u16,
    pub recent_documents: Vec<SidePanelItem>,
    pub search_results: Vec<SidePanelItem>,
    pub plugin_shortcuts: Vec<SidePanelItem>,
    pub active_section: SidePanelSection,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SidePanelSection {
    RecentDocuments,
    SearchResults,
    PluginShortcuts,
}

#[derive(Debug, Clone)]
pub struct SidePanelItem {
    pub label: String,
    pub detail: String,
}

impl SidePanel {
    pub fn new(collapsed: bool, width_pct: u16) -> Self {
        Self {
            collapsed,
            width_pct,
            recent_documents: Vec::new(),
            search_results: Vec::new(),
            plugin_shortcuts: Vec::new(),
            active_section: SidePanelSection::RecentDocuments,
        }
    }

    pub fn toggle(&mut self) {
        self.collapsed = !self.collapsed;
    }

    pub fn add_recent_document(&mut self, label: String, detail: String) {
        self.recent_documents.push(SidePanelItem { label, detail });
        // Keep last 20
        if self.recent_documents.len() > 20 {
            self.recent_documents.remove(0);
        }
    }

    pub fn set_search_results(&mut self, results: Vec<SidePanelItem>) {
        self.search_results = results;
    }
}

/// Input bar state.
pub struct InputBar {
    pub content: String,
    pub cursor_pos: usize,
    pub history: Vec<String>,
    pub history_index: Option<usize>,
}

impl InputBar {
    pub fn new() -> Self {
        Self {
            content: String::new(),
            cursor_pos: 0,
            history: Vec::new(),
            history_index: None,
        }
    }

    pub fn insert(&mut self, ch: char) {
        self.content.insert(self.cursor_pos, ch);
        self.cursor_pos += ch.len_utf8();
    }

    pub fn backspace(&mut self) {
        if self.cursor_pos > 0 {
            let prev = self.content[..self.cursor_pos]
                .chars()
                .last()
                .map_or(0, |c| c.len_utf8());
            self.cursor_pos -= prev;
            self.content.remove(self.cursor_pos);
        }
    }

    pub fn delete(&mut self) {
        if self.cursor_pos < self.content.len() {
            self.content.remove(self.cursor_pos);
        }
    }

    pub fn move_left(&mut self) {
        if self.cursor_pos > 0 {
            let prev = self.content[..self.cursor_pos]
                .chars()
                .last()
                .map_or(0, |c| c.len_utf8());
            self.cursor_pos -= prev;
        }
    }

    pub fn move_right(&mut self) {
        if self.cursor_pos < self.content.len() {
            let next = self.content[self.cursor_pos..]
                .chars()
                .next()
                .map_or(0, |c| c.len_utf8());
            self.cursor_pos += next;
        }
    }

    pub fn home(&mut self) {
        self.cursor_pos = 0;
    }

    pub fn end(&mut self) {
        self.cursor_pos = self.content.len();
    }

    pub fn take_content(&mut self) -> String {
        let content = self.content.clone();
        if !content.trim().is_empty() {
            self.history.push(content.clone());
        }
        self.content.clear();
        self.cursor_pos = 0;
        self.history_index = None;
        content
    }

    pub fn history_prev(&mut self) {
        if self.history.is_empty() {
            return;
        }
        let idx = match self.history_index {
            Some(i) if i > 0 => i - 1,
            Some(_) => return,
            None => self.history.len() - 1,
        };
        self.history_index = Some(idx);
        self.content = self.history[idx].clone();
        self.cursor_pos = self.content.len();
    }

    pub fn history_next(&mut self) {
        if let Some(idx) = self.history_index {
            if idx + 1 < self.history.len() {
                self.history_index = Some(idx + 1);
                self.content = self.history[idx + 1].clone();
                self.cursor_pos = self.content.len();
            } else {
                self.history_index = None;
                self.content.clear();
                self.cursor_pos = 0;
            }
        }
    }
}
