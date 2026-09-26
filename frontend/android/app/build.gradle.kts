plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// Firma release (scaffold, sin keystore en el repo):
// - Si existe `frontend/android/key.properties` se usa para firmar release.
// - Si no existe, release cae a la firma debug para no romper builds locales.
// Como crear el keystore (local, nunca versionar):
//   keytool -genkey -v -keystore frontend/android/app/upload-keystore.jks \
//     -keyalg RSA -keysize 2048 -validity 10000 -alias upload
// Y crear `frontend/android/key.properties` con:
//   storePassword=<password>
//   keyPassword=<password>
//   keyAlias=upload
//   storeFile=app/upload-keystore.jks  (relativo a frontend/android/)
val keystoreProperties = java.util.Properties()
val keystorePropertiesFile = rootProject.file("key.properties")
if (keystorePropertiesFile.exists()) {
    java.io.FileInputStream(keystorePropertiesFile).use { keystoreProperties.load(it) }
}

android {
    namespace = "com.bancaonline.banca_online"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.bancaonline.banca_online"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        create("release") {
            if (keystorePropertiesFile.exists()) {
                keyAlias = keystoreProperties["keyAlias"] as String
                keyPassword = keystoreProperties["keyPassword"] as String
                storeFile = rootProject.file(keystoreProperties["storeFile"] as String)
                storePassword = keystoreProperties["storePassword"] as String
            }
        }
    }

    buildTypes {
        // Q-T11 (rama refactorizacion-ui): el APK debug convive con el APK de
        // otra rama instalado por el dueno. Solo debug lleva sufijo, por lo que
        // el applicationId resultante es
        // `com.bancaonline.banca_online.refactorui` y no colisiona con el
        // release (`com.bancaonline.banca_online`). Release queda intacto.
        debug {
            applicationIdSuffix = ".refactorui"
        }
        release {
            // Con key.properties -> firma release real; sin el archivo ->
            // firma debug para no romper `flutter run --release` en local.
            signingConfig = if (keystorePropertiesFile.exists()) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
