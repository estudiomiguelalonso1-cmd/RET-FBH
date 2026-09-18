-- Esquema de la base de retenciones — Fiberhome Argentina S.A.
--
-- Escrito para SQLite, pero deliberadamente portable a PostgreSQL: sin tipos
-- propios de SQLite, claves foráneas explícitas, y los importes en NUMERIC.
-- Para mudarlo alcanza con cambiar INTEGER PRIMARY KEY AUTOINCREMENT por
-- GENERATED ALWAYS AS IDENTITY y TEXT por VARCHAR donde importe.
--
-- Decisiones que vienen de haber validado el motor contra el histórico 2026:
--
--   * Los comprobantes guardan el desglose completo (neto, IVA, otros conceptos,
--     total) y no sólo el neto, que es lo único que guarda el Excel de hoy. Sin el
--     total no se puede declarar el total aunque haga falta.
--   * Las retenciones tienen estado propio: una retención anulada NO consume el
--     mínimo no imponible del mes, y esa información hoy sólo existe como color de
--     celda en la planilla.
--   * Los acumulados mensuales son una vista, no una tabla: se derivan de las
--     retenciones y así no pueden quedar desincronizados.

PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------- proveedores
CREATE TABLE proveedores (
    cuit                TEXT PRIMARY KEY,
    razon_social        TEXT NOT NULL,
    tipo_persona        TEXT,          -- 'H' humana, 'J' juridica
    situacion_ganancias TEXT DEFAULT 'I',   -- 'I' inscripto, 'NI' no inscripto
    situacion_iva       TEXT,          -- '1' resp. inscripto, '3' exento, '4' monotributo
    situacion_ib        TEXT,          -- '1' local, '2' convenio multilateral, '4' no inscripto
    nro_inscripcion_ib  TEXT,          -- lo exige e-ARCIBA; para convenio es el CUIT

    -- Regimenes especiales que alcanzan al proveedor por su actividad. Hoy solo
    -- aplican a empresas de limpieza de inmuebles, investigacion y/o seguridad.
    retiene_iva_3164    INTEGER NOT NULL DEFAULT 0,   -- RG 3164, 10,5 % del neto
    retiene_suss_1556   INTEGER NOT NULL DEFAULT 0,   -- RG 1556,  6,0 % del neto
    retiene_suss_2682   INTEGER NOT NULL DEFAULT 0,   -- RG 2682, construccion
    tipo_obra           TEXT,                          -- ingenieria | arquitectura

    -- Jurisdiccion del proveedor. Fiberhome es de CABA, asi que a los proveedores
    -- de CABA les corresponde IIBB. A los de otra jurisdiccion solo si el servicio
    -- o el producto se prestó en CABA, y eso se marca por comprobante.
    jurisdiccion        TEXT,          -- CABA | PBA | OTRA

    domicilio           TEXT,
    creado              TEXT DEFAULT (datetime('now')),
    CHECK (length(cuit) = 11),
    CHECK (situacion_ganancias IN ('I', 'NI')),
    CHECK (situacion_iva IS NULL OR situacion_iva IN ('1', '3', '4')),
    CHECK (situacion_ib  IS NULL OR situacion_ib  IN ('1', '2', '4', '5')),
    CHECK (tipo_obra IS NULL OR tipo_obra IN ('ingenieria', 'arquitectura'))
);


-- --------------------------------------------------------------- comprobantes
CREATE TABLE comprobantes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cuit             TEXT NOT NULL REFERENCES proveedores(cuit),
    tipo             TEXT NOT NULL DEFAULT 'FC',   -- FC, ND, NC...
    letra            TEXT,                          -- A, B, C, M
    punto_venta      TEXT,
    numero           TEXT,
    numero_crudo     TEXT,      -- como figura en el origen, sin normalizar
    agrupa_varias    INTEGER NOT NULL DEFAULT 0,   -- la fila cubre mas de una factura
    fecha            TEXT,      -- ISO; NULL si el origen traia un rango
    fecha_cruda      TEXT,
    pzf              TEXT,      -- orden de pago / pedido interno

    neto             NUMERIC NOT NULL,
    importe_iva      NUMERIC,
    otros_conceptos  NUMERIC,
    total            NUMERIC,   -- neto + iva + otros; NULL mientras no se conozca

    moneda           TEXT NOT NULL DEFAULT 'ARS',
    tipo_cambio      NUMERIC,   -- si moneda <> ARS

    -- Para proveedores de fuera de CABA: si el servicio se presto en CABA igual
    -- corresponde retener IIBB.
    servicio_en_caba INTEGER NOT NULL DEFAULT 0,

    origen           TEXT,      -- de donde salio la fila, para auditar la migracion
    observacion      TEXT,
    creado           TEXT DEFAULT (datetime('now'))
);

CREATE INDEX ix_comprobantes_cuit  ON comprobantes(cuit);
CREATE INDEX ix_comprobantes_fecha ON comprobantes(fecha);


-- ---------------------------------------------------------------- retenciones
CREATE TABLE retenciones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    comprobante_id  INTEGER NOT NULL REFERENCES comprobantes(id) ON DELETE CASCADE,
    impuesto        TEXT NOT NULL,   -- ganancias | iibb_caba | iva | suss
    regimen         TEXT,            -- codigo de regimen del impuesto que corresponda
    periodo         TEXT NOT NULL,   -- AAAA-MM de la presentacion

    base_imponible  NUMERIC,
    alicuota        NUMERIC,
    monto           NUMERIC NOT NULL,

    fecha_retencion TEXT,
    nro_certificado TEXT,

    estado          TEXT NOT NULL DEFAULT 'practicada',
    motivo          TEXT,            -- por que esta anulada o no se practico

    origen          TEXT,
    creado          TEXT DEFAULT (datetime('now')),

    CHECK (impuesto IN ('ganancias', 'iibb_caba', 'iva', 'suss')),
    CHECK (estado   IN ('practicada', 'anulada', 'no_practicada'))
);

CREATE INDEX ix_retenciones_comprobante ON retenciones(comprobante_id);
CREATE INDEX ix_retenciones_periodo     ON retenciones(impuesto, periodo);


-- ----------------------------------------------------- tablas normativas (cache)
-- Ojo: el codigo de regimen NO identifica la regla por si solo. El regimen 116
-- cubre dos conceptos distintos con minimos no imponibles distintos ("Albacea,
-- mandatario", $16.830, y "Honorarios de director", $67.170). AFIP los distingue
-- con ID_ALICUOTA, que es lo que su propio simulador manda como vid_alicuota, asi
-- que forma parte de la clave.
CREATE TABLE regimenes_ganancias (
    id_alicuota      INTEGER NOT NULL,
    cod_regimen      INTEGER NOT NULL,
    situacion        TEXT NOT NULL,   -- I / NI
    tipo_persona     TEXT NOT NULL DEFAULT '',
    concepto         TEXT,
    anexo            TEXT,
    alicuota         NUMERIC,         -- 0 = "s/escala", ver escala_ganancias
    monto_no_sujeto  NUMERIC,
    monto_minimo     NUMERIC,
    sincronizado     TEXT,
    PRIMARY KEY (id_alicuota)
);

CREATE INDEX ix_regimenes_codigo
    ON regimenes_ganancias(cod_regimen, situacion, tipo_persona);

-- Escala progresiva del Anexo VIII de la RG 830, para los regimenes "s/escala".
CREATE TABLE escala_ganancias (
    vigencia_desde  TEXT NOT NULL,
    desde           NUMERIC NOT NULL,
    monto_fijo      NUMERIC NOT NULL,
    alicuota        NUMERIC NOT NULL,
    PRIMARY KEY (vigencia_desde, desde)
);

CREATE TABLE padron_iibb_caba (
    cuit             TEXT NOT NULL,
    vigencia_desde   TEXT NOT NULL,
    vigencia_hasta   TEXT NOT NULL,
    publicacion      TEXT,
    tipo_contr       TEXT,            -- 'D' directo CABA, 'C' convenio multilateral
    alic_percepcion  NUMERIC,
    alic_retencion   NUMERIC,
    razon_social     TEXT,
    PRIMARY KEY (cuit, vigencia_desde)
);


-- ---------------------------------------------------------------- exclusiones
-- Certificados de exclusion o de reduccion de alicuota. Tienen vigencia: hay que
-- controlar la fecha en cada calculo y avisar cuando estan por vencer, porque
-- seguir sin retener con un certificado vencido es tan error como retenerle a
-- quien esta excluido.
CREATE TABLE exclusiones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    cuit           TEXT NOT NULL REFERENCES proveedores(cuit),
    impuesto       TEXT NOT NULL,          -- ganancias | iibb_caba | iva | suss
    alcance        TEXT NOT NULL DEFAULT 'total',   -- total | parcial
    porcentaje     NUMERIC,                -- alicuota reducida, si es parcial
    vigencia_desde TEXT NOT NULL,
    vigencia_hasta TEXT,                   -- NULL = sin vencimiento conocido
    norma          TEXT,                   -- RG, resolucion o numero de certificado
    observacion    TEXT,
    creado         TEXT DEFAULT (datetime('now')),
    CHECK (impuesto IN ('ganancias', 'iibb_caba', 'iva', 'suss')),
    CHECK (alcance  IN ('total', 'parcial'))
);

CREATE INDEX ix_exclusiones_cuit ON exclusiones(cuit, impuesto);


-- -------------------------------------------------------------------- vistas
-- Lo que un proveedor ya consumio del minimo no imponible en el mes. Es la
-- consulta que el motor hace ANTES de calcular una retencion nueva.
--
-- Solo quedan afuera las ANULADAS. Un pago que no llego a generar retencion por
-- estar debajo del minimo ($240 en casi todos los regimenes) igual consumio
-- mínimo: el pago existio. Confundir las dos cosas fue lo que explico 13 de las
-- 21 diferencias iniciales al validar el motor contra el historico.
CREATE VIEW acumulados_mensuales AS
SELECT r.impuesto,
       r.periodo,
       c.cuit,
       r.regimen,
       COUNT(*)              AS cantidad,
       SUM(c.neto)           AS neto_acumulado,
       SUM(r.base_imponible) AS base_acumulada,
       SUM(r.monto)          AS retenido_acumulado
FROM   retenciones r
JOIN   comprobantes c ON c.id = r.comprobante_id
WHERE  r.estado <> 'anulada'
GROUP  BY r.impuesto, r.periodo, c.cuit, r.regimen;

-- Una fila por retencion con todo lo que hace falta para exportar o certificar.
CREATE VIEW retenciones_completas AS
SELECT r.id,
       r.impuesto, r.regimen, r.periodo, r.estado,
       r.base_imponible, r.alicuota, r.monto,
       r.fecha_retencion, r.nro_certificado,
       c.id AS comprobante_id, c.tipo, c.letra, c.punto_venta, c.numero,
       c.numero_crudo, c.agrupa_varias, c.fecha AS fecha_comprobante,
       c.neto, c.importe_iva, c.otros_conceptos, c.total, c.moneda,
       p.cuit, p.razon_social, p.situacion_iva, p.situacion_ib,
       p.nro_inscripcion_ib
FROM   retenciones r
JOIN   comprobantes c ON c.id = r.comprobante_id
JOIN   proveedores  p ON p.cuit = c.cuit;
