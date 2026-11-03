"""Streamlit CCv2 controls for moving, minimizing, and maximizing the run chat."""

from __future__ import annotations

import streamlit as st


_CHAT_WINDOW_CONTROLS = st.components.v2.component(
    "issue_triage_chat_window_controls",
    html="""
    <div class="chat-window-toolbar">
      <div class="chat-window-drag-handle" role="toolbar" aria-label="Chat window controls">
        <span class="chat-window-grip" aria-hidden="true">⠿</span>
        <span class="chat-window-title"></span>
      </div>
      <div class="chat-window-buttons">
        <button class="chat-window-minimize" type="button" aria-label="Minimize chat window" title="Minimize">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h10" /></svg>
        </button>
        <button class="chat-window-maximize" type="button" aria-label="Maximize chat window" title="Maximize">
          <svg class="maximize-icon" viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="1" /></svg>
          <svg class="restore-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3h8v8M3 5v8h8" /></svg>
        </button>
      </div>
    </div>
    """,
    css="""
    :host {
      display: block;
      flex: 0 0 44px !important;
      width: 100%;
      min-height: 44px;
      position: relative;
      z-index: 5;
      background: var(--st-background-color, #fff);
    }
    .chat-window-toolbar {
      display: flex;
      align-items: center;
      gap: .5rem;
      width: 100%;
      height: 42px;
      color: var(--st-text-color, #182522);
      font: 600 14px/1.2 var(--st-font, sans-serif);
    }
    .chat-window-drag-handle {
      display: flex;
      align-items: center;
      gap: .55rem;
      flex: 1 1 auto;
      min-width: 0;
      height: 100%;
      cursor: grab;
      touch-action: none;
      user-select: none;
    }
    .chat-window-drag-handle:active { cursor: grabbing; }
    .chat-window-grip { color: var(--st-secondary-text-color, #65736d); font-size: 1rem; }
    .chat-window-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chat-window-buttons { display: flex; align-items: center; gap: .2rem; }
    .chat-window-buttons button {
      display: grid;
      place-items: center;
      width: 32px;
      height: 30px;
      padding: 0;
      color: var(--st-text-color, #182522);
      background: transparent;
      border: 1px solid transparent;
      border-radius: var(--st-button-radius, 6px);
      cursor: pointer;
    }
    .chat-window-buttons button:hover {
      background: var(--st-secondary-background-color, #f2f6f2);
      border-color: var(--st-border-color, #d7ded9);
    }
    .chat-window-buttons svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.5; }
    .restore-icon { display: none; }
    .chat-window-maximize.is-maximized .maximize-icon { display: none; }
    .chat-window-maximize.is-maximized .restore-icon { display: block; }
    """,
    js="""
    export default function (component) {
      const { parentElement, data, setStateValue } = component;
      const componentHost = parentElement.host || parentElement;
      const toolbar = parentElement.querySelector(".chat-window-toolbar");
      const handle = parentElement.querySelector(".chat-window-drag-handle");
      const title = parentElement.querySelector(".chat-window-title");
      const minimizeButton = parentElement.querySelector(".chat-window-minimize");
      const maximizeButton = parentElement.querySelector(".chat-window-maximize");
      if (!toolbar || !handle || !title || !minimizeButton || !maximizeButton) return;

      // Keep the controls above the expanding chat body at every window size.
      componentHost.style.flex = "0 0 44px";
      componentHost.style.minHeight = "44px";
      componentHost.style.position = "relative";
      componentHost.style.zIndex = "5";

      title.textContent = data.title || "Run chat";
      const panelClass = `st-key-${data.panel_key}`;
      const bodyClass = `st-key-${data.body_key}`;
      const storageKey = `issue-triage-chat-window:${data.run_key}`;
      const findPanel = () => {
        let node = parentElement.host || parentElement;
        while (node) {
          if (node.classList && node.classList.contains(panelClass)) return node;
          node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
        }
        return null;
      };
      const panel = findPanel();
      if (!panel) return;
      const body = panel.querySelector(`.${bodyClass}`);
      if (!body) return;
      let dockChanged = false;
      if (panel.dataset.chatDockSide && panel.dataset.chatDockSide !== data.side) dockChanged = true;
      if (panel.dataset.chatWindowMode && panel.dataset.chatWindowMode !== data.window_mode) dockChanged = true;
      const dockRevision = String(data.dock_revision || 0);
      if (panel.dataset.chatDockRevision && panel.dataset.chatDockRevision !== dockRevision) dockChanged = true;
      panel.dataset.chatDockSide = data.side;
      panel.dataset.chatWindowMode = data.window_mode;
      panel.dataset.chatDockRevision = dockRevision;

      const loadState = () => {
        try {
          const value = JSON.parse(window.localStorage.getItem(storageKey) || "{}");
          if (dockChanged || (value.side && value.side !== data.side) || (value.window_mode && value.window_mode !== data.window_mode)) {
            dockChanged = true;
            return { side: data.side, window_mode: data.window_mode, rect: data.geometry || undefined };
          }
          return { ...value, side: data.side, window_mode: data.window_mode, rect: data.geometry || value.rect };
        } catch (_) {
          return { side: data.side, window_mode: data.window_mode, rect: data.geometry || undefined };
        }
      };
      let state = loadState();
      if (dockChanged) {
        state = { side: data.side, window_mode: data.window_mode, rect: data.geometry || undefined };
        try { window.localStorage.setItem(storageKey, JSON.stringify(state)); } catch (_) {}
      }
      let activeDragCleanup = null;
      let activeResizeCleanup = null;

      if (dockChanged) {
        ["position", "left", "top", "right", "bottom", "width", "height", "max-height", "max-width", "min-width", "min-height", "resize"].forEach(
          (property) => panel.style.removeProperty(property),
        );
        body.style.display = "";
        maximizeButton.classList.remove("is-maximized");
      }

      const saveState = () => {
        try { window.localStorage.setItem(storageKey, JSON.stringify(state)); } catch (_) {}
      };
      const readRect = () => {
        const rect = panel.getBoundingClientRect();
        return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
      };
      const layoutWidth = () => {
        const row = panel.closest('[data-testid="stHorizontalBlock"]');
        return row?.getBoundingClientRect().width || panel.parentElement?.clientWidth || window.innerWidth;
      };
      const reportLayout = (windowMode, side, rect) => {
        const width = layoutWidth();
        const layout = {
          window_mode: windowMode,
          side,
          rect: windowMode === "Floating" ? rect : null,
          width_ratio: rect?.width ? Math.max(.18, Math.min(.9, rect.width / width)) : null,
          height: rect?.height || null,
        };
        try { window.localStorage.setItem(storageKey, JSON.stringify({ ...layout, rect: layout.rect })); } catch (_) {}
        setStateValue("layout", layout);
      };
      const resizeDirections = {
        n: { left: "24%", right: "24%", top: "0", height: "10px", cursor: "ns-resize" },
        s: { left: "24%", right: "24%", bottom: "0", height: "10px", cursor: "ns-resize" },
        e: { top: "24%", bottom: "24%", right: "0", width: "10px", cursor: "ew-resize" },
        w: { top: "24%", bottom: "24%", left: "0", width: "10px", cursor: "ew-resize" },
        ne: { top: "0", right: "0", width: "18px", height: "18px", cursor: "nesw-resize" },
        nw: { top: "0", left: "0", width: "18px", height: "18px", cursor: "nwse-resize" },
        se: { bottom: "0", right: "0", width: "22px", height: "22px", cursor: "nwse-resize" },
        sw: { bottom: "0", left: "0", width: "18px", height: "18px", cursor: "nesw-resize" },
      };
      const resizeHandles = {};
      for (const [direction, geometry] of Object.entries(resizeDirections)) {
        let edge = panel.querySelector(`.chat-window-resize-${direction}`);
        if (!edge) {
          edge = document.createElement("button");
          edge.type = "button";
          edge.className = `chat-window-resize-handle chat-window-resize-${direction}`;
          edge.setAttribute("aria-label", `Resize chat window from ${direction.toUpperCase()} edge`);
          edge.title = "Drag to resize from this side";
          if (direction === "se") {
            edge.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true" width="16" height="16"><path d="M4 12 12 4M8 12l4-4M11 12l1-1" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
          }
          Object.assign(edge.style, {
            position: "absolute", zIndex: "10", display: "grid", placeItems: "center",
            padding: "0", border: "0", borderRadius: "5px", background: "transparent",
            color: "#587365", touchAction: "none", userSelect: "none",
          });
          panel.appendChild(edge);
        }
        for (const property of ["left", "right", "top", "bottom", "width", "height"]) edge.style[property] = "auto";
        Object.assign(edge.style, geometry);
        edge.onpointerenter = () => { edge.style.background = "rgba(35,107,82,.16)"; };
        edge.onpointerleave = () => { edge.style.background = "transparent"; };
        resizeHandles[direction] = edge;
      }
      const placeResizeHandles = () => {
        const display = state.maximized || state.minimized ? "none" : "grid";
        Object.values(resizeHandles).forEach((edge) => { edge.style.display = display; });
      };
      const applyRect = (rect) => {
        const floating = data.window_mode === "Floating";
        const containerWidth = layoutWidth();
        panel.style.position = floating ? "fixed" : "relative";
        panel.style.left = floating ? `${Math.max(0, Math.min(rect.left, window.innerWidth - 300))}px` : "auto";
        panel.style.top = floating ? `${Math.max(0, Math.min(rect.top, window.innerHeight - 260))}px` : "auto";
        panel.style.right = floating ? "auto" : "auto";
        panel.style.bottom = floating ? "auto" : "auto";
        const maxWidth = floating ? window.innerWidth - Math.max(0, rect.left) : containerWidth;
        panel.style.width = `${Math.max(300, Math.min(rect.width, maxWidth))}px`;
        panel.style.height = `${Math.max(260, Math.min(rect.height, floating ? window.innerHeight : window.innerHeight * 0.9))}px`;
        panel.style.maxHeight = floating ? "none" : "90vh";
        panel.style.maxWidth = floating ? `${Math.max(300, window.innerWidth - 24)}px` : `${containerWidth}px`;
        panel.style.minWidth = `${Math.min(300, containerWidth)}px`;
        panel.style.minHeight = "260px";
        panel.style.marginLeft = !floating && data.side === "Right" ? "auto" : "0";
        panel.style.marginRight = !floating && data.side === "Left" ? "auto" : "0";
      };
      const maximizedRect = () => {
        const header = document.querySelector('header[data-testid="stHeader"]');
        const hero = document.querySelector(".st-key-app_header") || document.querySelector(".hero");
        const headerBottom = header ? header.getBoundingClientRect().bottom : 0;
        const heroBottom = hero ? hero.getBoundingClientRect().bottom : 0;
        const top = Math.min(
          Math.max(headerBottom, heroBottom) + 12,
          Math.max(72, window.innerHeight - 180),
        );
        return {
          left: 12,
          top,
          width: window.innerWidth - 24,
          height: Math.max(96, window.innerHeight - top - 12),
        };
      };
      const applyState = () => {
        panel.style.zIndex = data.window_mode === "Floating" ? "10000" : "1";
        if (state.maximized) {
          if (data.window_mode === "Floating") {
            applyRect(maximizedRect());
          } else {
            applyRect({ left: 0, top: 0, width: panel.parentElement?.clientWidth || window.innerWidth, height: window.innerHeight * 0.82 });
          }
          body.style.display = "";
          maximizeButton.classList.add("is-maximized");
        } else {
          maximizeButton.classList.remove("is-maximized");
          if (state.rect) applyRect(state.rect);
        }
        body.style.display = state.minimized ? "none" : "";
        if (state.minimized) {
          panel.style.minHeight = "0";
          panel.style.height = "68px";
        }
        maximizeButton.setAttribute("aria-label", state.maximized ? "Restore chat window" : "Maximize chat window");
        maximizeButton.title = state.maximized ? "Restore" : "Maximize";
        placeResizeHandles();
      };
      if (data.window_mode !== "Floating" || state.rect || state.maximized || state.minimized) applyState();
      placeResizeHandles();
      const onWindowResize = () => { if (state.maximized) applyState(); };
      window.addEventListener("resize", onWindowResize);

      minimizeButton.onclick = () => {
        if (state.minimized) {
          state.minimized = false;
          applyState();
        } else {
          if (!state.maximized) state.rect = readRect();
          state.maximized = false;
          state.minimized = true;
          applyState();
        }
        saveState();
      };
      maximizeButton.onclick = () => {
        if (!state.maximized) {
          if (!state.minimized) state.rect = readRect();
          state.minimized = false;
          state.maximized = true;
        } else {
          state.maximized = false;
          state.minimized = false;
        }
        maximizeButton.setAttribute("aria-label", state.maximized ? "Restore chat window" : "Maximize chat window");
        maximizeButton.title = state.maximized ? "Restore" : "Maximize";
        applyState();
        saveState();
      };
      for (const [direction, edge] of Object.entries(resizeHandles)) edge.onpointerdown = (event) => {
        if (state.maximized || state.minimized) return;
        event.preventDefault();
        event.stopPropagation();
        const rect = readRect();
        const startX = event.clientX;
        const startY = event.clientY;
        const onMove = (moveEvent) => {
          const deltaX = moveEvent.clientX - startX;
          const deltaY = moveEvent.clientY - startY;
          const changesWest = direction.includes("w");
          const changesNorth = direction.includes("n");
          const changesWidth = direction.includes("e") || changesWest;
          const changesHeight = direction.includes("n") || direction.includes("s");
          const floating = data.window_mode === "Floating";
          const maxWidth = floating ? (changesWest ? rect.left + rect.width : window.innerWidth - rect.left) : layoutWidth() * .55;
          const width = changesWidth ? Math.max(300, Math.min(rect.width + (changesWest ? -deltaX : deltaX), maxWidth)) : rect.width;
          const maxHeight = floating ? (changesNorth ? rect.top + rect.height : window.innerHeight - rect.top) : window.innerHeight * 0.9;
          const height = changesHeight ? Math.max(260, Math.min(rect.height + (changesNorth ? -deltaY : deltaY), maxHeight)) : rect.height;
          const left = floating && changesWest ? rect.left + rect.width - width : rect.left;
          const top = floating && changesNorth ? rect.top + rect.height - height : rect.top;
          applyRect({ left, top, width, height });
        };
        const onUp = () => {
          const resizedRect = readRect();
          state.rect = data.window_mode === "Floating" ? resizedRect : undefined;
          saveState();
          const rowWidth = layoutWidth();
          reportLayout(data.window_mode, data.side, {
            ...resizedRect,
            width: resizedRect.width,
            width_ratio: resizedRect.width / rowWidth,
          });
          placeResizeHandles();
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          activeResizeCleanup = null;
        };
        activeResizeCleanup = () => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      };
      handle.onpointerdown = (event) => {
        if (event.target.closest("button")) return;
        event.preventDefault();
        event.stopPropagation();
        if (state.maximized) {
          state.maximized = false;
          body.style.display = "";
          maximizeButton.classList.remove("is-maximized");
          if (state.rect) applyRect(state.rect);
        }
        const rect = panel.getBoundingClientRect();
        const offsetX = event.clientX - rect.left;
        const offsetY = event.clientY - rect.top;
        panel.style.position = "fixed";
        panel.style.left = `${rect.left}px`;
        panel.style.top = `${rect.top}px`;
        panel.style.right = "auto";
        panel.style.bottom = "auto";
        panel.style.width = `${rect.width}px`;
        panel.style.height = `${rect.height}px`;
        panel.style.zIndex = "10000";
        const onMove = (moveEvent) => {
          const left = Math.max(0, Math.min(moveEvent.clientX - offsetX, window.innerWidth - rect.width));
          const top = Math.max(0, Math.min(moveEvent.clientY - offsetY, window.innerHeight - Math.min(rect.height, 68)));
          panel.style.position = "fixed";
          panel.style.left = `${left}px`;
          panel.style.top = `${top}px`;
          panel.style.right = "auto";
          panel.style.bottom = "auto";
          panel.style.width = `${rect.width}px`;
          panel.style.height = `${state.minimized ? 68 : rect.height}px`;
        };
        const onUp = (upEvent) => {
          const droppedRect = readRect();
          const pointerX = Math.min(window.innerWidth, Math.max(0, upEvent.clientX));
          const pointerY = Math.min(window.innerHeight, Math.max(0, upEvent.clientY));
          const edgeDistance = Math.min(pointerX, window.innerWidth - pointerX, pointerY, window.innerHeight - pointerY);
          let nextSide = data.side;
          if (edgeDistance <= 72) {
            const distances = {
              Left: pointerX,
              Right: window.innerWidth - pointerX,
              Top: pointerY,
              Bottom: window.innerHeight - pointerY,
            };
            nextSide = Object.entries(distances).sort((a, b) => a[1] - b[1])[0][0];
          }
          const dockToEdge = edgeDistance <= 72;
          const nextMode = dockToEdge ? "Docked" : "Floating";
          if (nextMode === "Floating") {
            state.rect = { ...droppedRect, left: Math.max(0, Math.min(droppedRect.left, window.innerWidth - 300)), top: Math.max(0, Math.min(droppedRect.top, window.innerHeight - 260)), width: rect.width, height: state.minimized ? Math.max(rect.height, 420) : rect.height };
          } else {
            state.rect = undefined;
          }
          state.maximized = false;
          state.minimized = false;
          state.side = nextSide;
          state.window_mode = nextMode;
          saveState();
          reportLayout(nextMode, nextSide, nextMode === "Floating" ? state.rect : droppedRect);
          if (dockToEdge) {
            ["position", "left", "top", "right", "bottom", "width", "height", "max-height", "max-width", "min-width", "min-height"].forEach((property) => panel.style.removeProperty(property));
          }
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
          activeDragCleanup = null;
        };
        activeDragCleanup = () => {
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      };

      return () => {
        if (activeDragCleanup) activeDragCleanup();
        if (activeResizeCleanup) activeResizeCleanup();
        window.removeEventListener("resize", onWindowResize);
      };
    }
    """,
)


def render_chat_window_controls(*, panel_key: str, body_key: str, run_key: str, title: str, side: str, window_mode: str = "Docked", dock_revision: int = 0) -> None:
    """Render an accessible drag handle with minimize and maximize buttons."""
    component_key = f"chat_window_controls_{run_key}"

    def apply_layout_change() -> None:
        component_state = st.session_state.get(component_key, {})
        layout = component_state.get("layout") if isinstance(component_state, dict) else getattr(component_state, "layout", None)
        if not isinstance(layout, dict):
            return
        next_mode = layout.get("window_mode", window_mode)
        next_side = layout.get("side", side)
        previous_side = st.session_state.get("chat_panel_side", "Right")
        changed_slot = next_mode != st.session_state.get("chat_panel_mode", "Docked") or next_side != previous_side
        st.session_state["chat_panel_mode"] = next_mode
        st.session_state["chat_panel_side"] = next_side
        ratio = layout.get("width_ratio")
        if ratio is not None:
            st.session_state["chat_panel_width_ratio"] = max(.18, min(.9, float(ratio)))
        if changed_slot and next_mode == "Docked":
            if next_side in ("Top", "Bottom"):
                st.session_state["chat_panel_width_ratio"] = 1.0
            elif previous_side in ("Top", "Bottom"):
                st.session_state["chat_panel_width_ratio"] = .285
        if layout.get("height") is not None:
            st.session_state["chat_panel_height"] = max(260, min(1200, int(layout["height"])))
        if next_mode == "Floating" and isinstance(layout.get("rect"), dict):
            st.session_state["chat_panel_geometry"] = layout["rect"]
        elif next_mode == "Docked":
            st.session_state.pop("chat_panel_geometry", None)
        if changed_slot:
            st.session_state["chat_panel_dock_revision"] = st.session_state.get("chat_panel_dock_revision", 0) + 1

    _CHAT_WINDOW_CONTROLS(
        key=component_key,
        data={
            "panel_key": panel_key,
            "body_key": body_key,
            "run_key": run_key,
            "title": title,
            "side": side,
            "window_mode": window_mode,
            "dock_revision": dock_revision,
            "geometry": st.session_state.get("chat_panel_geometry"),
        },
        on_layout_change=apply_layout_change,
        width="stretch",
        height=44,
    )
