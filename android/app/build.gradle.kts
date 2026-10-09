import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// The Python boundaries ship from the repository's single source tree.
val repositorySource = rootDir.resolve("../src")

android {
    namespace = "io.github.bastienstefani.iopenpod"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.bastienstefani.iopenpod"
        // Android 11 is the first release that lets an app with All files access
        // read and write removable Volumes through ordinary paths.
        minSdk = 30
        targetSdk = 36
        versionCode = 2
        versionName = "0.2.0-read-only-check"

        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        // Chaquopy requires the same Python minor version on the build machine.
        buildPython(
            providers.gradleProperty("iopenpod.buildPython").getOrElse("python3.12"),
        )
        pip {
            // Chaquopy's newest Android builds. The iPodDB, Device Registry and
            // Storage test suites pass against these versions.
            install("pillow==11.0.0")
            install("pycryptodome==3.21.0")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir(repositorySource)
            include(
                "iPodDB/**",
                "device_registry/**",
                "storage/**",
                "iOpenPod/android/**",
            )
            exclude("**/__pycache__/**", "iPodDB/tools/**")
        }
    }
}
