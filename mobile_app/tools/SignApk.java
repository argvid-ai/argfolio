import com.android.apksig.ApkSigner;
import com.android.apksig.ApkVerifier;
import java.io.File;
import java.io.FileInputStream;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.cert.X509Certificate;
import java.util.Collections;

/** Local preview signing. Production keys are supplied by the distributor. */
public class SignApk {
    public static void main(String[] args) throws Exception {
        String configuredPassword = System.getenv("PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD");
        if (configuredPassword == null || configuredPassword.isEmpty())
            throw new IllegalArgumentException("PAPERSCAN_PREVIEW_KEYSTORE_PASSWORD is required");
        char[] password = configuredPassword.toCharArray();
        KeyStore store = KeyStore.getInstance("JKS");
        try (FileInputStream in = new FileInputStream(args[0])) { store.load(in, password); }
        PrivateKey key = (PrivateKey)store.getKey("preview", password);
        X509Certificate cert = (X509Certificate)store.getCertificate("preview");
        ApkSigner.SignerConfig signer = new ApkSigner.SignerConfig.Builder("preview",key,Collections.singletonList(cert)).build();
        new ApkSigner.Builder(Collections.singletonList(signer)).setInputApk(new File(args[1]))
            .setOutputApk(new File(args[2])).setMinSdkVersion(23)
            .setV1SigningEnabled(true).setV2SigningEnabled(true).setV3SigningEnabled(true).build().sign();
        ApkVerifier.Result result = new ApkVerifier.Builder(new File(args[2])).build().verify();
        if (!result.isVerified()) throw new IllegalStateException(result.getErrors().toString());
        System.out.println("APK signature verified: v1="+result.isVerifiedUsingV1Scheme()+" v2="+result.isVerifiedUsingV2Scheme()+" v3="+result.isVerifiedUsingV3Scheme());
    }
}
