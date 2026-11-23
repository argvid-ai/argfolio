package com.cix.paperscan;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.view.Gravity;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayInputStream;
import java.net.URI;
import java.util.Locale;

/** Android window for the PaperScan workbench hosted on the board. */
public final class MainActivity extends Activity {
    private static final int BACKGROUND = Color.rgb(17, 21, 19);
    private static final int FOREGROUND = Color.rgb(238, 245, 236);
    private WebView web;
    private URI workbench;
    private TextView status;
    private String defaultAddress;

    @Override
    public void onCreate(Bundle bundle) {
        super.onCreate(bundle);
        getWindow().setStatusBarColor(BACKGROUND);
        getWindow().setNavigationBarColor(BACKGROUND);
        defaultAddress = getString(R.string.default_workbench);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BACKGROUND);
        root.setFitsSystemWindows(true);
        LinearLayout bar = new LinearLayout(this);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(14), dp(3), dp(6), dp(3));
        status = new TextView(this);
        status.setText("此芯扫描");
        status.setTextColor(FOREGROUND);
        status.setTextSize(16);
        bar.addView(status, new LinearLayout.LayoutParams(0, -2, 1));
        Button settings = new Button(this);
        settings.setText("连接设置");
        settings.setOnClickListener(view -> connectionDialog());
        bar.addView(settings);
        root.addView(bar);

        web = new WebView(this);
        web.setBackgroundColor(BACKGROUND);
        WebSettings settingsWeb = web.getSettings();
        settingsWeb.setJavaScriptEnabled(true);
        settingsWeb.setDomStorageEnabled(true);
        settingsWeb.setAllowFileAccess(false);
        settingsWeb.setAllowContentAccess(false);
        settingsWeb.setSupportZoom(false);
        settingsWeb.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleNavigation(request.getUrl().toString());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleNavigation(url);
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                if (url.startsWith("blob:") || url.equals("about:blank") || sameOrigin(url)) {
                    return null;
                }
                return new WebResourceResponse("text/plain", "UTF-8", new ByteArrayInputStream(new byte[0]));
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (sameOrigin(url)) status.setText("此芯扫描");
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showConnectionFailure();
            }

            @Override
            public void onReceivedSslError(WebView view, android.webkit.SslErrorHandler handler,
                                           android.net.http.SslError error) {
                handler.cancel();
                showConnectionFailure();
            }
        });
        web.setDownloadListener((url, ua, disposition, mime, length) -> download(url));
        root.addView(web, new LinearLayout.LayoutParams(-1, 0, 1));
        setContentView(root);

        android.content.SharedPreferences preferences = getPreferences(MODE_PRIVATE);
        String saved = preferences.getString("workbench_address", "");
        if (saved.isEmpty()) {
            String previous = preferences.getString("workbench_v12", "");
            if (!previous.equals(getString(R.string.previous_lan_workbench))) saved = previous;
        }
        if (saved.isEmpty()) saved = defaultAddress;
        if (saved.isEmpty()) connectionDialog();
        else connect(saved);
    }

    private int dp(int value) {
        return Math.round(getResources().getDisplayMetrics().density * value);
    }

    private boolean handleNavigation(String url) {
        if (!sameOrigin(url)) return true;
        if (Uri.parse(url).getPath().startsWith("/download/")) {
            download(url);
            return true;
        }
        return false;
    }

    private int port(URI uri) {
        return uri.getPort() >= 0 ? uri.getPort() : ("https".equals(uri.getScheme()) ? 443 : 80);
    }

    private boolean sameOrigin(String value) {
        try {
            URI uri = URI.create(value);
            return workbench != null && workbench.getScheme().equals(uri.getScheme())
                    && workbench.getHost().equalsIgnoreCase(uri.getHost())
                    && port(workbench) == port(uri);
        } catch (IllegalArgumentException error) {
            return false;
        }
    }

    private void connect(String address) {
        try {
            URI uri = URI.create(address.trim());
            if (!("http".equals(uri.getScheme()) || "https".equals(uri.getScheme()))
                    || uri.getHost() == null || uri.getUserInfo() != null || uri.getQuery() != null
                    || uri.getFragment() != null || port(uri) < 1 || port(uri) > 65535
                    || !(uri.getPath().isEmpty() || uri.getPath().equals("/"))) {
                throw new IllegalArgumentException();
            }
            workbench = URI.create(uri.getScheme() + "://" + uri.getRawAuthority());
            getPreferences(MODE_PRIVATE).edit().putString("workbench_address", workbench.toString()).apply();
            status.setText("正在连接板子…");
            web.loadUrl(workbench + "/");
        } catch (IllegalArgumentException error) {
            toast("请输入完整的 HTTP 或 HTTPS 工作台地址。");
        }
    }

    private void connectionDialog() {
        final EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(workbench == null ? defaultAddress : workbench.toString());
        input.setHint("https://工作台地址");
        new AlertDialog.Builder(this)
                .setTitle("连接设置")
                .setMessage("使用公网工作台地址时，可以通过 Wi-Fi 或手机流量连接。打开后输入板子配对码。")
                .setView(input)
                .setPositiveButton("连接", (dialog, which) -> connect(input.getText().toString()))
                .setNegativeButton("取消", null)
                .show();
    }

    private void showConnectionFailure() {
        status.setText("工作台连接失败");
        web.loadDataWithBaseURL(null,
                "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<style>body{background:#111513;color:#eef5ec;font:16px sans-serif;padding:24px}p{line-height:1.7}</style>"
                + "<h2>暂时无法连接工作台</h2><p>检查手机网络、电脑到板子的网线和工作台地址，"
                + "然后在右上角的「连接设置」中重试。</p>",
                "text/html", "UTF-8", null);
    }

    private void download(String url) {
        Uri uri = Uri.parse(url);
        if (!sameOrigin(url) || uri.getPath() == null || !uri.getPath().startsWith("/download/")) {
            toast("无效的下载地址。");
            return;
        }
        try {
            String name = uri.getLastPathSegment().replaceAll("[\\\\/:*?\"<>|]", "_");
            DownloadManager.Request request = new DownloadManager.Request(uri);
            request.setTitle(name);
            request.setDescription("此芯扫描");
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            String extension = name.toLowerCase(Locale.ROOT);
            request.setMimeType(extension.endsWith(".pdf") ? "application/pdf"
                    : extension.endsWith(".zip") ? "application/zip" : "text/plain");
            // The bundled LAN HTTPS endpoint uses an app-specific trust anchor.
            // Android's separate system DownloadManager does not inherit that anchor.
            if ("https".equals(uri.getScheme())) {
                saveHttpsFile(url, name, requestMime(extension));
                return;
            }
            if (Build.VERSION.SDK_INT >= 29) {
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            } else {
                request.setDestinationInExternalFilesDir(this, Environment.DIRECTORY_DOWNLOADS, name);
            }
            ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(request);
            toast("开始下载，可从系统下载通知打开文件。");
        } catch (Exception error) {
            toast("下载失败，请检查网络或剩余空间。");
        }
    }

    private static final int SAVE_FILE = 12;
    private String pendingDownload;

    private String requestMime(String extension) {
        if (extension.endsWith(".pdf")) return "application/pdf";
        if (extension.endsWith(".zip")) return "application/zip";
        return "text/plain";
    }

    private void saveHttpsFile(String url, String name, String type) {
        pendingDownload = url;
        android.content.Intent intent = new android.content.Intent(android.content.Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(android.content.Intent.CATEGORY_OPENABLE);
        intent.setType(type);
        intent.putExtra(android.content.Intent.EXTRA_TITLE, name);
        startActivityForResult(intent, SAVE_FILE);
    }

    @Override
    protected void onActivityResult(int code, int result, android.content.Intent data) {
        super.onActivityResult(code, result, data);
        if (code != SAVE_FILE) return;
        String url = pendingDownload;
        pendingDownload = null;
        if (result != RESULT_OK || data == null || url == null) return;
        Uri target = data.getData();
        new Thread(() -> {
            java.net.HttpURLConnection connection = null;
            try {
                connection = (java.net.HttpURLConnection) URI.create(url).toURL().openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(10000);
                connection.setReadTimeout(30000);
                if (connection.getResponseCode() != 200) throw new java.io.IOException("Download unavailable");
                try (java.io.InputStream source = connection.getInputStream();
                     java.io.OutputStream destination = getContentResolver().openOutputStream(target)) {
                    if (destination == null) throw new java.io.IOException("No destination");
                    byte[] buffer = new byte[65536];
                    int count;
                    while ((count = source.read(buffer)) != -1) destination.write(buffer, 0, count);
                }
                runOnUiThread(() -> toast("已保存到所选位置。"));
            } catch (Exception error) {
                runOnUiThread(() -> toast("保存失败，请检查连接后重试。"));
            } finally {
                if (connection != null) connection.disconnect();
            }
        }, "PaperScan download").start();
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.stopLoading();
            web.destroy();
        }
        super.onDestroy();
    }
}
