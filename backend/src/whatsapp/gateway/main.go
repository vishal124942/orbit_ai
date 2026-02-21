package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// ── JSON Protocol (same as gateway_v3.js) ───────────────────────────────────

type OutEvent struct {
	Type string      `json:"type"`
	Data interface{} `json:"data,omitempty"`
	// Flat fields for specific events
	ID        string      `json:"id,omitempty"`
	Status    string      `json:"status,omitempty"`
	User      interface{} `json:"user,omitempty"`
	From      string      `json:"from,omitempty"`
	PushName  string      `json:"pushName,omitempty"`
	Text      string      `json:"text,omitempty"`
	MediaPath string      `json:"mediaPath,omitempty"`
	MediaType string      `json:"mediaType,omitempty"`
	Timestamp interface{} `json:"timestamp,omitempty"`
	IsGroup   bool        `json:"isGroup,omitempty"`
	FromMe    bool        `json:"fromMe,omitempty"`
	Success   bool        `json:"success,omitempty"`
	Message   string      `json:"message,omitempty"`
}

type InCommand struct {
	Type      string `json:"type"`
	ID        int64  `json:"id"`
	To        string `json:"to"`
	Text      string `json:"text"`
	Media     string `json:"media"`
	MediaType string `json:"mediaType"`
	MessageID string `json:"messageId"`
	Emoji     string `json:"emoji"`
}

type ContactInfo struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Notify   string `json:"notify"`
	PushName string `json:"pushName"`
	IsLid    bool   `json:"isLid"`
}

type HistoryMessage struct {
	ID        string `json:"id"`
	From      string `json:"from"`
	PushName  string `json:"pushName"`
	Text      string `json:"text"`
	FromMe    bool   `json:"fromMe"`
	Timestamp int64  `json:"timestamp"`
}

// ── Globals ─────────────────────────────────────────────────────────────────

var (
	client    *whatsmeow.Client
	outMu     sync.Mutex // Protect stdout writes
	contacts  = make(map[string]ContactInfo)
	contactMu sync.Mutex
)

const MAX_HISTORY_PER_CONTACT = 50

// ── Output helpers ──────────────────────────────────────────────────────────

func emit(v interface{}) {
	outMu.Lock()
	defer outMu.Unlock()
	data, err := json.Marshal(v)
	if err != nil {
		return
	}
	fmt.Println(string(data))
}

func logErr(msg string) {
	fmt.Fprintln(os.Stderr, "[Gateway] "+msg)
}

// ── Event Handler ───────────────────────────────────────────────────────────

type eventHandler struct{}

func (h *eventHandler) HandleEvent(rawEvt interface{}) {
	switch evt := rawEvt.(type) {

	case *events.QR:
		// Emit QR codes for pairing
		for _, code := range evt.Codes {
			emit(map[string]string{"type": "qr", "data": code})
		}

	case *events.PairSuccess:
		logErr(fmt.Sprintf("Successfully paired with %s", evt.ID))

	case *events.Connected:
		logErr("Connection opened successfully")
		emit(map[string]interface{}{
			"type":   "connection",
			"status": "open",
			"user": map[string]string{
				"id":   client.Store.ID.String(),
				"name": client.Store.PushName,
			},
		})
		// Request contacts after connection
		go func() {
			time.Sleep(2 * time.Second)
			sendContacts()
		}()

	case *events.Disconnected:
		logErr("Connection closed")
		emit(map[string]interface{}{
			"type":   "connection",
			"status": "close",
		})

	case *events.LoggedOut:
		logErr("Logged out! Clearing auth and exiting for re-pair...")
		emit(map[string]interface{}{
			"type":   "connection",
			"status": "close",
		})
		// Clean up auth — Python bridge will restart us
		if client.Store.ID != nil {
			_ = client.Store.Delete(context.Background())
		}
		os.Exit(0)

	case *events.PushNameSetting:
		// Own push name changed
		logErr(fmt.Sprintf("Push name set to: %s", evt.Action.GetName()))

	case *events.Message:
		handleMessage(evt)

	case *events.HistorySync:
		handleHistorySync(evt)

	case *events.Contact:
		handleContactEvent(evt)
	}
}

// ── Message Handler ─────────────────────────────────────────────────────────

func handleMessage(evt *events.Message) {
	info := evt.Info

	// Skip status broadcasts
	if info.Chat.Server == "broadcast" || info.Chat.Server == "status" {
		return
	}

	// Extract text
	text := ""
	if evt.Message.GetConversation() != "" {
		text = evt.Message.GetConversation()
	} else if evt.Message.GetExtendedTextMessage() != nil {
		text = evt.Message.GetExtendedTextMessage().GetText()
	} else if evt.Message.GetImageMessage() != nil {
		text = evt.Message.GetImageMessage().GetCaption()
		if text == "" {
			text = "[Sent an image]"
		}
	} else if evt.Message.GetVideoMessage() != nil {
		text = evt.Message.GetVideoMessage().GetCaption()
		if text == "" {
			text = "[Sent a video]"
		}
	} else if evt.Message.GetAudioMessage() != nil {
		text = "[Sent an audio]"
	} else if evt.Message.GetStickerMessage() != nil {
		text = "[Sticker]"
	} else if evt.Message.GetDocumentMessage() != nil {
		text = "[Sent a document]"
	}

	jid := info.Chat.String()
	isGroup := info.Chat.Server == "g.us"

	// Update contact cache
	if info.PushName != "" && !isGroup {
		contactMu.Lock()
		if c, ok := contacts[jid]; !ok || c.Name == "" {
			contacts[jid] = ContactInfo{
				ID:       jid,
				PushName: info.PushName,
				Notify:   info.PushName,
			}
		}
		contactMu.Unlock()
	}

	// Emit to Python
	emit(map[string]interface{}{
		"type":      "message",
		"id":        info.ID,
		"from":      jid,
		"pushName":  info.PushName,
		"text":      text,
		"mediaPath": nil,
		"mediaType": nil,
		"timestamp": info.Timestamp.Unix(),
		"isGroup":   isGroup,
		"fromMe":    info.IsFromMe,
	})
}

// ── History Sync Handler ────────────────────────────────────────────────────

func handleHistorySync(evt *events.HistorySync) {
	data := evt.Data
	logErr(fmt.Sprintf("History sync: type=%s, conversations=%d",
		data.GetSyncType(), len(data.GetConversations())))

	byContact := make(map[string][]HistoryMessage)

	for _, conv := range data.GetConversations() {
		jid := conv.GetID()
		if jid == "" || strings.Contains(jid, "broadcast") || strings.Contains(jid, "status@") {
			continue
		}

		// Update contacts from conversation metadata
		displayName := conv.GetDisplayName()
		if displayName != "" {
			contactMu.Lock()
			contacts[jid] = ContactInfo{
				ID:   jid,
				Name: displayName,
			}
			contactMu.Unlock()
		}

		for _, histMsg := range conv.GetMessages() {
			msg := histMsg.GetMessage()
			if msg == nil || msg.GetMessage() == nil {
				continue
			}

			key := msg.GetKey()
			if key == nil {
				continue
			}

			// Extract text
			text := extractTextFromMessage(msg.GetMessage())
			if text == "" {
				continue
			}

			remoteJid := key.GetRemoteJID()
			if remoteJid == "" {
				remoteJid = jid
			}

			hm := HistoryMessage{
				ID:        key.GetID(),
				From:      remoteJid,
				PushName:  msg.GetPushName(),
				Text:      text,
				FromMe:    key.GetFromMe(),
				Timestamp: int64(msg.GetMessageTimestamp()),
			}

			byContact[remoteJid] = append(byContact[remoteJid], hm)
		}
	}

	// Cap per contact and emit
	var capped []HistoryMessage
	for _, msgs := range byContact {
		// Sort by timestamp desc and cap
		if len(msgs) > MAX_HISTORY_PER_CONTACT {
			msgs = msgs[:MAX_HISTORY_PER_CONTACT]
		}
		capped = append(capped, msgs...)
	}

	if len(capped) > 0 {
		logErr(fmt.Sprintf("History sync: emitting %d msgs (capped at %d/contact) from %d contacts",
			len(capped), MAX_HISTORY_PER_CONTACT, len(byContact)))
		emit(map[string]interface{}{
			"type": "history_messages",
			"data": capped,
		})
	}

	// Also emit contacts
	go func() {
		time.Sleep(500 * time.Millisecond)
		sendContacts()
	}()
}

func extractTextFromMessage(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}
	if msg.GetConversation() != "" {
		return msg.GetConversation()
	}
	if msg.GetExtendedTextMessage() != nil {
		return msg.GetExtendedTextMessage().GetText()
	}
	if msg.GetImageMessage() != nil && msg.GetImageMessage().GetCaption() != "" {
		return msg.GetImageMessage().GetCaption()
	}
	if msg.GetVideoMessage() != nil && msg.GetVideoMessage().GetCaption() != "" {
		return msg.GetVideoMessage().GetCaption()
	}
	return ""
}

// ── Contact Sync ────────────────────────────────────────────────────────────

func handleContactEvent(evt *events.Contact) {
	jid := evt.JID.String()
	contactMu.Lock()
	name := ""
	if evt.Action != nil {
		name = evt.Action.GetFullName()
		if name == "" {
			name = evt.Action.GetFirstName()
		}
	}
	contacts[jid] = ContactInfo{
		ID:   jid,
		Name: name,
	}
	contactMu.Unlock()
}

func sendContacts() {
	contactMu.Lock()
	defer contactMu.Unlock()

	var cleaned []ContactInfo
	for _, c := range contacts {
		if c.ID == "" {
			continue
		}
		// Skip system JIDs
		if strings.Contains(c.ID, "status@") || strings.Contains(c.ID, "broadcast") || strings.Contains(c.ID, "newsletter") {
			continue
		}
		// Accept @s.whatsapp.net, @g.us, @lid
		if !strings.HasSuffix(c.ID, "@s.whatsapp.net") && !strings.HasSuffix(c.ID, "@g.us") && !strings.HasSuffix(c.ID, "@lid") {
			continue
		}

		name := c.Name
		if name == "" {
			name = c.Notify
		}
		if name == "" {
			name = c.PushName
		}

		cleaned = append(cleaned, ContactInfo{
			ID:       c.ID,
			Name:     name,
			Notify:   c.Notify,
			PushName: c.PushName,
			IsLid:    strings.HasSuffix(c.ID, "@lid"),
		})
	}

	logErr(fmt.Sprintf("Sending %d contacts (from %d cached)", len(cleaned), len(contacts)))
	emit(map[string]interface{}{
		"type": "contacts",
		"data": cleaned,
	})
}

// ── Command Processing (stdin) ──────────────────────────────────────────────

func processCommands(ctx context.Context) {
	scanner := bufio.NewScanner(os.Stdin)
	// Increase buffer size for large JSON payloads
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)

	for scanner.Scan() {
		select {
		case <-ctx.Done():
			return
		default:
		}

		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}

		var cmd InCommand
		if err := json.Unmarshal([]byte(line), &cmd); err != nil {
			logErr(fmt.Sprintf("Invalid command JSON: %v", err))
			continue
		}

		go executeCommand(cmd)
	}
}

func executeCommand(cmd InCommand) {
	if client == nil || !client.IsConnected() {
		emit(map[string]interface{}{
			"type":    "error",
			"id":      cmd.ID,
			"message": "Not connected",
		})
		return
	}

	var err error
	switch cmd.Type {
	case "send_message":
		err = handleSendMessage(cmd)
	case "react":
		err = handleReaction(cmd)
	case "delete_message":
		err = handleDeleteMessage(cmd)
	case "get_contacts":
		sendContacts()
	default:
		err = fmt.Errorf("unknown command type: %s", cmd.Type)
	}

	if err != nil {
		emit(map[string]interface{}{
			"type":    "error",
			"id":      cmd.ID,
			"message": err.Error(),
		})
	} else {
		emit(map[string]interface{}{
			"type":    "ack",
			"id":      cmd.ID,
			"success": true,
		})
	}
}

func handleSendMessage(cmd InCommand) error {
	jid, err := parseJID(cmd.To)
	if err != nil {
		return err
	}

	// Send typing indicator
	_ = client.SendChatPresence(context.Background(), jid, types.ChatPresenceComposing, types.ChatPresenceMediaText)

	var msg *waProto.Message

	if cmd.Media != "" && cmd.MediaType != "" {
		// Media message — for now send as text with media reference
		// Full media upload can be added later
		text := cmd.Text
		if text == "" {
			text = fmt.Sprintf("[Sent a %s]", cmd.MediaType)
		}
		msg = &waProto.Message{
			Conversation: proto.String(text),
		}
	} else {
		// Plain text message
		msg = &waProto.Message{
			Conversation: proto.String(cmd.Text),
		}
	}

	resp, err := client.SendMessage(context.Background(), jid, msg)
	if err != nil {
		return fmt.Errorf("send failed: %v", err)
	}

	// Stop typing
	_ = client.SendChatPresence(context.Background(), jid, types.ChatPresencePaused, types.ChatPresenceMediaText)

	logErr(fmt.Sprintf("Message sent to %s (id=%s)", jid, resp.ID))
	return nil
}

func handleReaction(cmd InCommand) error {
	jid, err := parseJID(cmd.To)
	if err != nil {
		return err
	}

	msg := &waProto.Message{
		ReactionMessage: &waProto.ReactionMessage{
			Key: &waProto.MessageKey{
				RemoteJID: proto.String(jid.String()),
				ID:        proto.String(cmd.MessageID),
				FromMe:    proto.Bool(false),
			},
			Text:              proto.String(cmd.Emoji),
			SenderTimestampMS: proto.Int64(time.Now().UnixMilli()),
		},
	}

	_, err = client.SendMessage(context.Background(), jid, msg)
	return err
}

func handleDeleteMessage(cmd InCommand) error {
	jid, err := parseJID(cmd.To)
	if err != nil {
		return err
	}

	_, err = client.SendMessage(context.Background(), jid, client.BuildRevoke(jid, types.EmptyJID, cmd.MessageID))
	return err
}

func parseJID(raw string) (types.JID, error) {
	if !strings.Contains(raw, "@") {
		raw = raw + "@s.whatsapp.net"
	}
	jid, err := types.ParseJID(raw)
	if err != nil {
		return types.JID{}, fmt.Errorf("invalid JID %q: %v", raw, err)
	}
	return jid, nil
}

// ── Main ────────────────────────────────────────────────────────────────────

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: gateway <auth_dir>")
		os.Exit(1)
	}
	authDir := os.Args[1]

	// Ensure auth dir exists
	if err := os.MkdirAll(authDir, 0755); err != nil {
		logErr(fmt.Sprintf("Failed to create auth dir: %v", err))
		os.Exit(1)
	}

	logErr(fmt.Sprintf("Starting whatsmeow gateway, auth=%s", authDir))

	// Quiet logger for whatsmeow internals
	dbLog := waLog.Noop
	clientLog := waLog.Stdout("Gateway", "WARN", true)

	// Open SQLite store for auth state
	dbPath := fmt.Sprintf("file:%s/whatsmeow.db?_journal_mode=WAL&_foreign_keys=on", authDir)
	container, err := sqlstore.New(context.Background(), "sqlite3", dbPath, dbLog)
	if err != nil {
		logErr(fmt.Sprintf("Failed to open auth DB: %v", err))
		os.Exit(1)
	}

	// Get first device or create new
	device, err := container.GetFirstDevice(context.Background())
	if err != nil {
		logErr(fmt.Sprintf("Failed to get device: %v", err))
		os.Exit(1)
	}

	client = whatsmeow.NewClient(device, clientLog)
	handler := &eventHandler{}
	client.AddEventHandler(handler.HandleEvent)

	// Enable history sync
	client.EnableAutoReconnect = true
	client.AutoTrustIdentity = true

	// Connect
	if client.Store.ID == nil {
		// No session — need QR code
		logErr("No existing session, generating QR code...")
		qrChan, _ := client.GetQRChannel(context.Background())
		err = client.Connect()
		if err != nil {
			logErr(fmt.Sprintf("Connect failed: %v", err))
			os.Exit(1)
		}

		// Process QR events
		go func() {
			for qrEvt := range qrChan {
				if qrEvt.Event == "code" {
					emit(map[string]string{"type": "qr", "data": qrEvt.Code})
				} else if qrEvt.Event == "timeout" {
					logErr("QR code timeout — exiting")
					os.Exit(1)
				} else if qrEvt.Event == "success" {
					logErr("QR pairing successful!")
				}
			}
		}()
	} else {
		// Existing session — reconnect
		logErr(fmt.Sprintf("Connecting with existing session for %s...", client.Store.ID))
		err = client.Connect()
		if err != nil {
			logErr(fmt.Sprintf("Connect failed: %v", err))
			os.Exit(1)
		}
	}

	// Request full app state sync for contacts
	go func() {
		time.Sleep(3 * time.Second)
		if client.IsConnected() {
			logErr("Requesting app state sync for contacts...")
			err := client.FetchAppState(context.Background(), appstate.WAPatchCriticalUnblockLow, false, false)
			if err != nil {
				logErr(fmt.Sprintf("App state sync error (non-fatal): %v", err))
			}
		}
	}()

	// Process stdin commands in background
	ctx, cancel := context.WithCancel(context.Background())
	go processCommands(ctx)

	// Wait for signal
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan

	logErr("Shutting down gracefully...")
	cancel()
	client.Disconnect()
	logErr("Goodbye!")
}
