import SwiftUI
import WebKit
import UniformTypeIdentifiers

@main
struct PaperScanApp: App {
    var body: some Scene { WindowGroup { ContentView() } }
}

struct SharedDocument: Identifiable { let id = UUID(); let url: URL }

final class Browser: NSObject, ObservableObject, WKNavigationDelegate, WKDownloadDelegate {
    @Published var message = "输入板子地址，连接你的扫描板。"
    @Published var document: SharedDocument?
    let web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
    private var origin: URL?
    override init() {
        super.init()
        web.navigationDelegate = self
        web.isOpaque = false
        web.backgroundColor = UIColor(red: 0.96, green: 0.96, blue: 0.93, alpha: 1)
        web.allowsBackForwardNavigationGestures = true
    }
    func connect(_ address: String) {
        guard let u = URL(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["http", "https"].contains(u.scheme ?? ""), u.host != nil,
              u.user == nil, u.password == nil, u.query == nil, u.fragment == nil,
              u.path.isEmpty || u.path == "/" else {
            message = "请输入完整工作台地址，例如 http://设备地址:8088"; return
        }
        origin = u
        UserDefaults.standard.set(address, forKey: "board")
        message = "正在连接板子…"
        web.load(URLRequest(url: u, timeoutInterval: 15))
    }
    private func sameOrigin(_ url: URL) -> Bool {
        guard let base = origin else { return false }
        return url.scheme == base.scheme && url.host == base.host && url.port == base.port
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url, sameOrigin(url) else {
            decisionHandler(.cancel); return
        }
        decisionHandler(url.path.hasPrefix("/download/") ? .download : .allow)
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (navigationResponse.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(disposition.hasPrefix("attachment") ? .download : .allow)
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        guard let url = response.url, sameOrigin(url), (response as? HTTPURLResponse)?.statusCode == 200 else {
            message = "下载失败，请检查配对码和网络。"; completionHandler(nil); return
        }
        let name = URL(fileURLWithPath: suggestedFilename).lastPathComponent
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let dest = folder.appendingPathComponent(name)
            destinations[ObjectIdentifier(download)] = dest
            completionHandler(dest)
        } catch { message = "无法保存下载文件。"; completionHandler(nil) }
    }
    private var destinations: [ObjectIdentifier: URL] = [:]
    func downloadDidFinish(_ download: WKDownload) {
        if let file = destinations.removeValue(forKey: ObjectIdentifier(download)) {
            message = "下载完成，选择存储到文件或分享。"
            document = SharedDocument(url: file)
        }
    }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        destinations.removeValue(forKey: ObjectIdentifier(download)); message = "下载失败，请检查网络后重试。"
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { message = "已连接 · \(origin?.host ?? "板子")" }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        message = "连接失败，请检查同一局域网、板子地址和板端服务。"
    }
}

struct BoardWebView: UIViewRepresentable {
    let browser: Browser
    func makeUIView(context: Context) -> WKWebView { browser.web }
    func updateUIView(_ view: WKWebView, context: Context) {}
}
struct ShareSheet: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }
    func updateUIViewController(_ view: UIActivityViewController, context: Context) {}
}
struct ContentView: View {
    @StateObject private var browser = Browser()
    @State private var settings = false
    @State private var address = UserDefaults.standard.string(forKey: "board") ?? "http://"
    var body: some View {
        VStack(spacing: 0) {
            HStack { Text("此芯扫描").font(.headline); Spacer(); Button("连接设置") { settings = true } }
                .padding(.horizontal).padding(.vertical, 10)
            Text(browser.message).font(.caption).foregroundStyle(.secondary).padding(.horizontal).padding(.bottom, 8)
            BoardWebView(browser: browser)
        }
        .background(Color(red: 0.96, green: 0.96, blue: 0.93))
        .tint(Color(red: 0.14, green: 0.31, blue: 0.23))
        .sheet(isPresented: $settings) {
            NavigationStack {
                Form {
                    Section("连接扫描板") {
                        TextField("http://板子IP:8088", text: $address).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        Text("手机和板子需要在同一局域网。连接后输入板端配对码。").font(.footnote)
                        Button("连接") { settings = false; browser.connect(address) }
                    }
                }.navigationTitle("连接设置")
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("关闭") { settings = false } } }
            }
        }
        .sheet(item: $browser.document) { ShareSheet(url: $0.url) }
        .onAppear { if address == "http://" { settings = true } else { browser.connect(address) } }
    }
}
